# Copyright (c) 2026, company@bwhstudios.com and Contributors

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.data import flt

from commera.api.payments import make_sales_invoice
from commera.tests.test_admin_orders import make_test_sales_order
from commera.utils import can_return, update_sales_order_ecommerce_status

try:
	from erpnext.selling.doctype.sales_order.mapper import make_delivery_note
	from erpnext.stock.doctype.delivery_note.mapper import make_sales_return
except ImportError:
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

return_items = frappe.get_module("commera.api.return").return_items


def make_paid_order():
	sales_order = make_test_sales_order()
	sales_invoice = make_sales_invoice(sales_order.name, ignore_permissions=True)
	sales_invoice.flags.ignore_permissions = True
	sales_invoice.insert()
	sales_invoice.submit()
	return sales_order.reload()


def deliver(sales_order, submit=True):
	delivery_note = make_delivery_note(sales_order.name)
	delivery_note.flags.ignore_permissions = True
	delivery_note.insert()
	if submit:
		delivery_note.submit()
	return delivery_note


def book_parcel(sales_order_name: str, status: str, **values):
	"""The carrier booking of the order, as the carrier reports it."""
	frappe.get_doc(
		{
			"doctype": "Shipping Request",
			"name": frappe.generate_hash(length=10),
			"ref_doctype": "Sales Order",
			"ref_docname": sales_order_name,
			"status": status,
			**values,
		}
	).db_insert()


class TestOrderStatus(IntegrationTestCase):
	def make_return(self, delivery_note, qty=None):
		sales_return = make_sales_return(delivery_note.name)
		if qty is not None:
			sales_return.items[0].qty = -qty
		sales_return.flags.ignore_permissions = True
		sales_return.insert()
		sales_return.submit()
		return sales_return

	def status_of(self, sales_order):
		update_sales_order_ecommerce_status(sales_order.name)
		return frappe.db.get_value("Sales Order", sales_order.name, "custom_ecommerce_status")

	def test_a_paid_order_is_received_not_delivered(self):
		sales_order = make_paid_order()
		self.assertEqual(flt(sales_order.per_billed), 100)
		self.assertEqual(self.status_of(sales_order), "Order Received")

	def test_a_draft_delivery_note_means_the_order_is_being_packed(self):
		sales_order = make_paid_order()
		deliver(sales_order, submit=False)
		self.assertEqual(self.status_of(sales_order), "Preparing for Shipment")

	def test_without_a_carrier_a_submitted_delivery_note_is_the_delivery(self):
		sales_order = make_paid_order()
		deliver(sales_order)
		self.assertEqual(self.status_of(sales_order), "Delivered")

	def test_a_booked_parcel_holds_the_order_until_the_carrier_moves_it(self):
		sales_order = make_paid_order()
		deliver(sales_order)
		book_parcel(sales_order.name, "Ready To Ship")
		self.assertEqual(self.status_of(sales_order), "Preparing for Shipment")

	def test_the_carrier_status_sets_the_rung(self):
		for carrier_status, expected in (
			("In Transit", "Shipped"),
			("Out For Delivery", "Shipped"),
			("Delivered", "Delivered"),
			("RTO", "Returned"),
		):
			with self.subTest(carrier_status=carrier_status):
				sales_order = make_paid_order()
				deliver(sales_order)
				book_parcel(sales_order.name, carrier_status)
				self.assertEqual(self.status_of(sales_order), expected)

	def test_a_cancelled_booking_falls_back_to_the_delivery_note(self):
		sales_order = make_paid_order()
		deliver(sales_order)
		book_parcel(sales_order.name, "Cancelled")
		self.assertEqual(self.status_of(sales_order), "Delivered")

	def test_returning_part_of_an_order_is_a_partial_return(self):
		sales_order = make_paid_order()
		delivery_note = deliver(sales_order)
		self.make_return(delivery_note, qty=1)
		self.assertEqual(self.status_of(sales_order), "Partially Returned")

	def test_returning_everything_is_a_return(self):
		sales_order = make_paid_order()
		delivery_note = deliver(sales_order)
		self.make_return(delivery_note)
		self.assertEqual(flt(frappe.db.get_value("Sales Order", sales_order.name, "per_delivered")), 0)
		self.assertEqual(self.status_of(sales_order), "Returned")

	def test_a_cancelled_order_is_cancelled(self):
		sales_order = make_test_sales_order()
		sales_order.cancel()
		self.assertEqual(self.status_of(sales_order), "Cancelled")


class TestReturnWindow(IntegrationTestCase):
	def test_a_paid_undelivered_order_cannot_be_returned(self):
		self.assertFalse(can_return(make_paid_order().name, 30))

	def test_the_window_opens_at_delivery(self):
		sales_order = make_paid_order()
		delivery_note = deliver(sales_order, submit=False)
		self.assertFalse(can_return(sales_order.name, 30))

		delivery_note.submit()
		self.assertTrue(can_return(sales_order.name, 30))
		self.assertFalse(can_return(sales_order.name, 0))


class TestReturnItems(IntegrationTestCase):
	def setUp(self):
		frappe.db.set_single_value("Commera Settings", "return_period", 30)

	def request_return(self, sales_order):
		return_items(sales_order.name, [{"item_code": sales_order.items[0].item_code, "reason": "Too big"}])

	def test_an_undelivered_order_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.request_return(make_paid_order())

	def test_a_delivered_order_drafts_a_return_note(self):
		sales_order = make_paid_order()
		deliver(sales_order)
		self.request_return(sales_order)
		self.assertTrue(
			frappe.db.exists(
				"Delivery Note Item",
				{"against_sales_order": sales_order.name, "docstatus": 0, "qty": ["<", 0]},
			)
		)

	def test_the_same_item_cannot_be_returned_twice(self):
		sales_order = make_paid_order()
		deliver(sales_order)
		self.request_return(sales_order)
		with self.assertRaises(frappe.ValidationError):
			self.request_return(sales_order)
