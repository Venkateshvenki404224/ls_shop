# Copyright (c) 2026, company@bwhstudios.com and contributors
# For license information, please see license.txt

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_purchase_order
from frappe import _
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.model.document import Document
from frappe.utils import get_url_to_form

from commera.api.payments import system_user_session
from commera.qikink.client import QikinkClient, QikinkRefusedError, QikinkUnavailableError
from commera.qikink.items import get_qikink_lines, get_qikink_supplier
from commera.qikink.payload import QikinkDataError, get_order_body

PUSH_JOB = "commera.qikink.jobs.push_qikink_order"
MAX_ATTEMPTS = 3
PUSHABLE_STATES = ("Queued", "Failed")
PURCHASE_ORDER_SAVEPOINT = "qikink_purchase_order"


class PurchaseOrderRefusedError(frappe.ValidationError):
	"""ERPNext refused the drop-ship Purchase Order, for example for a supplier setup error."""


class QikinkOrder(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		attempts: DF.Int
		error: DF.SmallText | None
		gateway: DF.Literal["", "COD", "Prepaid"]
		order_number: DF.Data | None
		purchase_order: DF.Link | None
		qikink_order_id: DF.Data | None
		sales_order: DF.Link
		status: DF.Literal["Queued", "Pushed", "Failed", "Needs Attention", "Completed", "Cancelled"]
		total_order_value: DF.Currency
	# end: auto-generated types

	@property
	def is_pushable(self) -> bool:
		return self.status in PUSHABLE_STATES

	@property
	def has_failed_before(self) -> bool:
		# Each failure records its error, and only a push that reaches Qikink clears it.
		return bool(self.error)

	def onload(self):
		self.set_onload("is_pushable", self.is_pushable)

	def validate(self):
		# Qikink takes only [a-z0-9_] in an order number, at most 15 characters.
		self.order_number = self.name.lower().replace("-", "")

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
		# Lock the row, then read it again: a cancel or another push may have changed it meanwhile.
		frappe.db.get_value(self.doctype, self.name, "name", for_update=True)
		self.reload()
		if not self.is_pushable:
			return
		try:
			self.send_to_qikink()
		except QikinkUnavailableError as error:
			self.count_failed_attempt(str(error))
		except (PurchaseOrderRefusedError, QikinkDataError, QikinkRefusedError) as error:
			self.fail(str(error))
		self.save(ignore_permissions=True)

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
