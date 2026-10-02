# Copyright (c) 2026, company@bwhstudios.com and Contributors

import re
from unittest.mock import patch

import frappe
import requests

from commera.commera_ecommerce.doctype.qikink_order.qikink_order import QikinkOrder
from commera.tests.qikink import QIKINK_ORDER_ID
from commera.tests.test_qikink_sync import QikinkSyncTestCase


class QikinkCancelTestCase(QikinkSyncTestCase):
	def place_approved_cod_order(self):
		"""A COD order has no Sales Invoice, so only its Purchase Order can block its cancel."""
		sales_order = self.place_cod_order()
		sales_order.submit()
		return sales_order

	def place_pushed_cod_order(self):
		return self.push(self.place_approved_cod_order())

	def get_purchase_order(self, qikink_order):
		return frappe.get_doc("Purchase Order", qikink_order.purchase_order)

	def get_sales_order(self, qikink_order):
		return frappe.get_doc("Sales Order", qikink_order.sales_order)


class TestQikinkSalesOrderCancel(QikinkCancelTestCase):
	def test_a_sales_order_with_a_submitted_qikink_purchase_order_cannot_be_cancelled(self):
		"""ERPNext would unlink the Purchase Order and cancel the Sales Order, and Qikink would ship it."""
		pushed_order = self.place_pushed_cod_order()
		waiting_order = self.push_into_timeouts(self.place_approved_cod_order())

		for qikink_order in (pushed_order, waiting_order):
			with self.subTest(status=qikink_order.status):
				with self.assertRaisesRegex(
					frappe.ValidationError,
					re.escape(f"Cancel the Qikink Purchase Order {qikink_order.purchase_order} first."),
				):
					self.get_sales_order(qikink_order).cancel()

	def test_a_sales_order_cancelled_before_its_push_sends_nothing(self):
		"""Staff approve a COD order and cancel it before the push job runs."""
		sales_order = self.place_approved_cod_order()

		sales_order.cancel()

		qikink_order = self.push(sales_order)
		self.assertEqual(qikink_order.status, "Cancelled")
		self.assertEqual(self.qikink.requests, [])
		self.assertFalse(frappe.db.exists("Purchase Order Item", {"sales_order": sales_order.name}))
		self.assertEqual(self.get_alerts(qikink_order), {"desk": [], "email": []})


class TestQikinkCancelSync(QikinkCancelTestCase):
	"""Staff cancel the order in the Qikink dashboard, and the sync takes the cancel on."""

	def test_a_cancel_at_qikink_cancels_the_purchase_order_and_frees_the_sales_order(self):
		qikink_order = self.sync_status(self.place_pushed_cod_order(), "Cancelled")

		self.assertEqual((qikink_order.qikink_status, qikink_order.status), ("Cancelled", "Cancelled"))
		self.assertEqual(self.get_purchase_order(qikink_order).docstatus, 2)
		self.get_sales_order(qikink_order).cancel()
		self.assertEqual(frappe.db.get_value("Sales Order", qikink_order.sales_order, "docstatus"), 2)

	def test_sync_now_cancels_the_purchase_order_for_staff_who_may_not_cancel_it(self):
		qikink_order = self.place_pushed_cod_order()
		self.qikink.report(qikink_order.qikink_order_id, "Cancelled")
		frappe.set_user(self.notify_users[0])

		self.click("sync", qikink_order)

		frappe.set_user("Administrator")
		self.assertEqual(self.get_purchase_order(qikink_order).docstatus, 2)

	def test_the_sync_stops_reading_a_cancelled_order(self):
		self.sync_status(self.place_pushed_order(), "Cancelled")
		self.qikink.requests.clear()

		self.run_sync()

		self.assertEqual(self.listed_ids, [])


class TestQikinkPurchaseOrderCancel(QikinkCancelTestCase):
	def test_the_purchase_order_of_an_order_qikink_has_cannot_be_cancelled(self):
		qikink_order = self.place_pushed_order()

		for status in ("Live", "On Hold"):
			with self.subTest(status=status):
				qikink_order = self.sync_status(qikink_order, status)

				with self.assertRaisesRegex(
					frappe.ValidationError,
					re.escape(
						f"Cancel {qikink_order.order_number} in the Qikink dashboard first. "
						"Then click Sync now on the Qikink Order."
					),
				):
					self.get_purchase_order(qikink_order).cancel()

	def test_the_purchase_order_of_a_push_that_did_not_reach_qikink_can_be_cancelled_at_once(self):
		waiting_order = self.push_into_timeouts(self.place_prepaid_order())
		self.refuse_next_create()
		failed_order = self.push(self.place_prepaid_order())

		for qikink_order in (waiting_order, failed_order):
			with self.subTest(status=qikink_order.status):
				self.get_purchase_order(qikink_order).cancel()

				qikink_order.reload()
				self.assertEqual(qikink_order.status, "Cancelled")
				self.assertEqual(self.get_purchase_order(qikink_order).docstatus, 2)

	def test_a_push_that_timed_out_but_reached_qikink_cannot_be_cancelled_here(self):
		qikink_order = self.push_into_timeouts(self.place_prepaid_order())
		# The create reached Qikink, but its reply did not reach Commera.
		self.qikink.add_order(qikink_order.order_number, order_id=QIKINK_ORDER_ID + 1)

		with self.assertRaisesRegex(
			frappe.ValidationError,
			re.escape(f"Cancel {qikink_order.order_number} in the Qikink dashboard first."),
		):
			self.get_purchase_order(qikink_order).cancel()

	def test_the_cancel_of_a_push_that_timed_out_waits_while_qikink_is_down(self):
		qikink_order = self.push_into_timeouts(self.place_prepaid_order())
		self.qikink.list_replies = [requests.Timeout("Read timed out")]

		with self.assertRaisesRegex(frappe.ValidationError, "Qikink did not answer: Timeout"):
			self.get_purchase_order(qikink_order).cancel()

	def test_a_push_that_lands_while_the_cancel_waits_for_the_row_lock_stops_the_cancel(self):
		qikink_order = self.push_into_timeouts(self.place_prepaid_order())
		lock_qikink_order = QikinkOrder.reload_for_update

		def push_first(locked_order):
			"""The push held the lock, and reached Qikink before it let go."""
			frappe.db.set_value(
				"Qikink Order",
				locked_order.name,
				{"status": "Pushed", "qikink_order_id": str(QIKINK_ORDER_ID)},
			)
			lock_qikink_order(locked_order)

		with (
			patch.object(QikinkOrder, "reload_for_update", push_first),
			self.assertRaisesRegex(frappe.ValidationError, "in the Qikink dashboard first"),
		):
			self.get_purchase_order(qikink_order).cancel()

	def test_a_push_that_runs_after_the_cancel_sends_nothing(self):
		sales_order = self.place_prepaid_order()
		qikink_order = self.push_into_timeouts(sales_order)
		self.get_purchase_order(qikink_order).cancel()
		self.qikink.requests.clear()

		self.push(sales_order)
		self.run_sync()

		self.assertEqual(self.qikink.requests, [])
		self.assertEqual(self.get_qikink_order(sales_order).status, "Cancelled")
		self.assertEqual(
			frappe.get_all("Purchase Order Item", {"sales_order": sales_order.name}, pluck="docstatus"), [2]
		)
