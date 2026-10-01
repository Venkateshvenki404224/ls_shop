# Copyright (c) 2026, company@bwhstudios.com and contributors
# For license information, please see license.txt

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_purchase_order
from frappe.model.document import Document

from commera.api.payments import system_user_session
from commera.qikink.client import QikinkClient
from commera.qikink.items import get_qikink_lines, get_qikink_supplier
from commera.qikink.payload import get_order_body

PUSH_JOB = "commera.qikink.jobs.push_qikink_order"


class QikinkOrder(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		gateway: DF.Literal["", "COD", "Prepaid"]
		order_number: DF.Data | None
		purchase_order: DF.Link | None
		qikink_order_id: DF.Data | None
		sales_order: DF.Link
		status: DF.Literal["Queued", "Pushed", "Failed", "Needs Attention", "Completed", "Cancelled"]
		total_order_value: DF.Currency
	# end: auto-generated types

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

	def push(self) -> None:
		"""Send the Qikink lines to Qikink, with the one drop-ship Purchase Order made for them."""
		if self.status != "Queued":
			return
		if not self.purchase_order:
			self.make_drop_ship_purchase_order()
		body = get_order_body(self)
		self.gateway = body["gateway"]
		self.total_order_value = body["total_order_value"]
		reply = QikinkClient().create_order(body)
		self.qikink_order_id = reply["order_id"]
		self.status = "Pushed"
		self.save(ignore_permissions=True)

	def make_drop_ship_purchase_order(self) -> None:
		"""Buy the Qikink lines from Qikink as Administrator: the approver of the order may not buy."""
		supplier = get_qikink_supplier()
		qikink_lines = get_qikink_lines(frappe.get_doc("Sales Order", self.sales_order))
		with system_user_session():
			purchase_order = make_purchase_order(
				self.sales_order,
				selected_items=[{"item_code": row.item_code, "supplier": supplier} for row in qikink_lines],
			)[0]
			purchase_order.submit()
		self.purchase_order = purchase_order.name
