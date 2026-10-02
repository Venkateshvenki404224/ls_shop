# Copyright (c) 2026, company@bwhstudios.com and contributors
# For license information, please see license.txt

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_purchase_order
from frappe import _
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.model.document import Document
from frappe.utils import cstr, get_datetime, get_url_to_form, now_datetime, validate_url

from commera.api.payments import system_user_session
from commera.qikink.client import QikinkClient, QikinkRefusedError, QikinkUnavailableError
from commera.qikink.items import get_qikink_lines, get_qikink_supplier
from commera.qikink.payload import QikinkDataError, get_order_body
from commera.qikink.status import RemoteStatus, get_remote_status
from commera.utils import update_sales_order_ecommerce_status

PUSH_JOB = "commera.qikink.jobs.push_qikink_order"
MAX_ATTEMPTS = 3
PUSHABLE_STATES = ("Queued", "Failed")
SYNCABLE_STATES = ("Pushed", "Needs Attention")
PURCHASE_ORDER_SAVEPOINT = "qikink_purchase_order"
WEB_SCHEMES = ("http", "https")


class PurchaseOrderRefusedError(frappe.ValidationError):
	"""ERPNext refused the drop-ship Purchase Order, for example for a supplier setup error."""


class QikinkOrder(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from bwh_shipping.bwh_shipping.doctype.shipping_tracking_event.shipping_tracking_event import (
			ShippingTrackingEvent,
		)
		from frappe.types import DF

		attempts: DF.Int
		awb: DF.Data | None
		courier: DF.Data | None
		error: DF.SmallText | None
		gateway: DF.Literal["", "COD", "Prepaid"]
		last_synced_on: DF.Datetime | None
		order_number: DF.Data | None
		purchase_order: DF.Link | None
		qikink_order_id: DF.Data | None
		qikink_status: DF.Data | None
		sales_order: DF.Link
		status: DF.Literal["Queued", "Pushed", "Failed", "Needs Attention", "Completed", "Cancelled"]
		total_order_value: DF.Currency
		tracking_events: DF.Table[ShippingTrackingEvent]
		tracking_link: DF.Data | None
	# end: auto-generated types

	@property
	def is_pushable(self) -> bool:
		return self.status in PUSHABLE_STATES

	@property
	def is_syncable(self) -> bool:
		return self.status in SYNCABLE_STATES

	@property
	def has_failed_before(self) -> bool:
		# Each failure records its error, and only a push that reaches Qikink clears it.
		return bool(self.error)

	@property
	def ecommerce_status(self) -> str:
		"""Where the parcel is for the shopper: the last Qikink status that moves the order. A problem moves nothing."""
		for event in reversed(self.tracking_events):
			remote_status = get_remote_status(event.status)
			if remote_status and remote_status.ecommerce_status:
				return remote_status.ecommerce_status
		return "Order Received"

	def onload(self):
		self.set_onload("is_pushable", self.is_pushable)
		self.set_onload("is_syncable", self.is_syncable)

	def validate(self):
		# Qikink takes only [a-z0-9_] in an order number, at most 15 characters.
		self.order_number = self.name.lower().replace("-", "")

	def on_update(self):
		# The shopper's status reads only the Qikink statuses, so the push and the AWB cannot move it.
		if self.qikink_status and self.has_value_changed("qikink_status"):
			update_sales_order_ecommerce_status(self.sales_order)

	def queue_push(self) -> None:
		frappe.enqueue(
			PUSH_JOB,
			qikink_order=self.name,
			job_id=f"qikink_push::{self.name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	@frappe.whitelist()
	def requeue_push(self) -> None:
		"""Push again once staff fix the order. The attempts start over."""
		if not self.is_pushable:
			frappe.throw(_("Only a Queued or Failed Qikink Order can be pushed."))
		self.status = "Queued"
		self.attempts = 0
		self.save()
		self.queue_push()

	def push(self) -> None:
		"""Send the Qikink lines to Qikink, with the one drop-ship Purchase Order made for them."""
		self.reload_for_update()
		if not self.is_pushable:
			return
		try:
			self.send_to_qikink()
		except QikinkUnavailableError as error:
			self.count_failed_attempt(str(error))
		except (PurchaseOrderRefusedError, QikinkDataError, QikinkRefusedError) as error:
			self.fail(str(error))
		self.save(ignore_permissions=True)

	def reload_for_update(self) -> None:
		"""Lock the row, then read it again: a cancel, a push or a sync may have changed it meanwhile."""
		frappe.db.get_value(self.doctype, self.name, "name", for_update=True)
		self.reload()

	@frappe.whitelist(methods=["POST"])
	def sync(self) -> None:
		"""Read the order at Qikink now, without waiting for the sync run."""
		# Frappe checks only read permission before a whitelisted method.
		self.check_permission("write")
		if not self.is_syncable:
			frappe.throw(_("Only a Pushed or Needs Attention Qikink Order can be synced."))
		client = QikinkClient()
		if not client.has_order_list:
			frappe.throw(_("The Qikink sandbox has no order list, so Sync now works on live only."))
		try:
			remote_orders = client.get_orders([self.qikink_order_id])
		except QikinkUnavailableError as error:
			frappe.throw(str(error))
		self.apply_order_list(remote_orders)

	def apply_order_list(self, remote_orders: dict[str, dict]) -> None:
		"""Take on what the Qikink order list, as orders by order id, reports for the order."""
		self.reload_for_update()
		if not self.is_syncable:
			return
		remote_order = remote_orders.get(self.qikink_order_id)
		if not remote_order:
			frappe.throw(_("The Qikink order list has no order {0}.").format(self.qikink_order_id))
		remote_status = get_remote_status(remote_order["status"])
		if not remote_status:
			self.log_error(
				_("Unknown Qikink status"),
				_("Qikink reports the status {0}, which Commera does not know.").format(
					remote_order["status"]
				),
			)
			return
		# One alert for each problem: a sync that finds the same problem again sends none.
		is_new_problem = remote_status.is_problem and self.status != "Needs Attention"
		self.record_shipment(remote_order)
		self.last_synced_on = now_datetime()
		if remote_order["status"] != self.qikink_status:
			self.record_status_change(remote_order, remote_status)
		self.save(ignore_permissions=True)
		# After the save: an order that fails to save is tried again, and must not alert again.
		if is_new_problem:
			self.send_alert(
				_("Qikink order {0} needs attention").format(self.order_number),
				_("Qikink reports {0}.").format(self.qikink_status),
			)

	def record_shipment(self, remote_order: dict) -> None:
		shipping = remote_order.get("shipping") or {}
		self.awb = shipping.get("awb") or None
		self.courier = cstr(shipping.get("courier_provider_name")).strip() or None
		# The shopper's order page links to it, so only a web address may go there.
		tracking_link = cstr(shipping.get("tracking_link")).strip()
		self.tracking_link = tracking_link if validate_url(tracking_link, valid_schemes=WEB_SCHEMES) else None

	def record_status_change(self, remote_order: dict, remote_status: RemoteStatus) -> None:
		"""Add the new Qikink status to the history, and move the order on."""
		self.qikink_status = remote_order["status"]
		delivered_on = remote_order.get("delivered_on") if remote_status.is_delivery else None
		self.append(
			"tracking_events",
			{"timestamp": get_datetime(delivered_on or self.last_synced_on), "status": self.qikink_status},
		)
		self.status = remote_status.state
		if remote_status.is_delivery:
			self.deliver_purchase_order()

	def deliver_purchase_order(self) -> None:
		"""Deliver each line through the ERPNext drop-ship action, which updates the Sales Order."""
		# As Administrator: the staff member who clicks "Sync now" may not change Purchase Orders.
		with system_user_session():
			purchase_order = frappe.get_doc("Purchase Order", self.purchase_order)
			undelivered_lines = [
				{"name": row.name, "qty_change": row.qty - row.received_qty}
				for row in purchase_order.items
				if row.received_qty < row.qty
			]
			if undelivered_lines:
				purchase_order.update_dropship_received_qty(undelivered_lines)

	def send_to_qikink(self) -> None:
		if not self.purchase_order:
			self.make_drop_ship_purchase_order()
		body = get_order_body(self)
		self.gateway = body["gateway"]
		self.total_order_value = body["total_order_value"]
		client = QikinkClient()
		reply = self.get_earlier_qikink_order(client) or client.create_order(body)
		self.qikink_order_id = reply["order_id"]
		self.status = "Pushed"
		self.error = None

	def get_earlier_qikink_order(self, client: QikinkClient) -> dict | None:
		"""The order a failed push may have made, though its reply never came."""
		if not self.has_failed_before or not client.has_order_list:
			return None
		return client.get_order(self.order_number)

	def count_failed_attempt(self, error: str) -> None:
		"""Leave the order Queued for the next sync run, until the attempts run out."""
		self.attempts += 1
		self.status = "Queued"
		self.error = error
		if self.attempts >= MAX_ATTEMPTS:
			self.fail(error)

	def fail(self, error: str) -> None:
		self.status = "Failed"
		self.error = error
		self.send_alert(_("The push of Qikink order {0} failed").format(self.order_number), error)

	def send_alert(self, subject: str, message: str) -> None:
		"""Tell each of the Notify Users, in Desk and by email."""
		notify_users = [row.user for row in frappe.get_cached_doc("Qikink Settings").notify_users]
		if not notify_users:
			return
		emails = frappe.get_all("User", filters={"name": ["in", notify_users], "enabled": 1}, pluck="email")
		reference = {"document_type": self.doctype, "document_name": self.name}
		# An Alert notification sends no email of its own, so the one below is the only one.
		enqueue_create_notification(
			emails, {"type": "Alert", "subject": subject, "email_content": message, **reference}
		)
		try:
			frappe.sendmail(
				recipients=emails,
				subject=subject,
				template="new_notification",
				args={
					"body_content": subject,
					"description": message,
					"doc_link": get_url_to_form(self.doctype, self.name),
				},
				reference_doctype=self.doctype,
				reference_name=self.name,
			)
		except frappe.OutgoingEmailError:
			# A site without outgoing email still records the failure and the Desk notification.
			self.log_error(_("Qikink alert email failed"))

	def make_drop_ship_purchase_order(self) -> None:
		"""Buy the Qikink lines from Qikink as Administrator: the approver of the order may not buy."""
		supplier = get_qikink_supplier()
		qikink_lines = get_qikink_lines(frappe.get_doc("Sales Order", self.sales_order))
		# ERPNext may refuse in on_submit, after it saved the Purchase Order: undo that save too.
		frappe.db.savepoint(PURCHASE_ORDER_SAVEPOINT)
		try:
			with system_user_session():
				purchase_order = make_purchase_order(
					self.sales_order,
					selected_items=[
						{"item_code": row.item_code, "supplier": supplier} for row in qikink_lines
					],
				)[0]
				purchase_order.submit()
		except frappe.ValidationError as error:
			frappe.db.rollback(save_point=PURCHASE_ORDER_SAVEPOINT)
			raise PurchaseOrderRefusedError(str(error)) from error
		self.purchase_order = purchase_order.name
