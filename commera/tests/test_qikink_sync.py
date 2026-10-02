# Copyright (c) 2026, company@bwhstudios.com and Contributors

from unittest.mock import patch

import frappe
import requests
from frappe.utils import get_datetime, now_datetime

from commera.tests import create_staff
from commera.tests.qikink import ORDER_LIST_PATH, configure_qikink, make_reply
from commera.tests.test_qikink_push_failures import QikinkFailureTestCase

AWB = "77908747221"
TRACKING_LINK = f"https://courierupdates.com/?awb={AWB}"
# Qikink sends the courier with a trailing space.
SHIPPED = {"awb": AWB, "tracking_link": TRACKING_LINK, "courier_provider_name": "Bluedart Surface "}
# The Qikink statuses of the spec, spelled as Qikink spells them.
NORMAL_STATUSES = (
	"Live",
	"To be Printed",
	"Partitally Picklisted",
	"Partially Printed",
	"Printed",
	"S-Printed",
	"Stitched/QC",
	"Dispatch Ready",
	"Manifested",
	"PIcked Up",
	"In-Transit",
	"Out for Delivery",
	"Delivery rescheduled",
	"Not attempted",
	"Consignee unavailable",
	"Misrouted",
	"ODA",
	"OTP not shared",
	"Residence / office closed",
)
PROBLEM_STATUSES = (
	"Out Of Stock",
	"Live-OOS",
	"On Hold",
	"Incorrect/incomplete Address",
	"Exception",
	"Lost",
	"Refused to accept",
	"Open delivery refused",
	"Maximum attempts reached",
	"OTP verification cancelled",
	"RTO Initiated",
	"Reverse Pickup Initiated",
)


class QikinkSyncTestCase(QikinkFailureTestCase):
	"""Qikink is live, because the sandbox has no order list."""

	def setUp(self):
		super().setUp()
		configure_qikink(
			self,
			supplier=self.supplier,
			sandbox=0,
			notify_users=[{"user": user} for user in self.notify_users],
		)
		# Each order takes 2 of the warehouse item, and a test may place a dozen orders.
		frappe.db.set_value("Bin", {"item_code": self.warehouse_item}, "actual_qty", 100)

	def place_pushed_order(self):
		return self.push(self.place_prepaid_order())

	def sync_status(self, qikink_order, status: str, **shipping):
		"""Sync the status Qikink reports, and hand back the Qikink Order."""
		self.qikink.report(qikink_order.qikink_order_id, status, **shipping)
		self.run_sync()
		return frappe.get_doc("Qikink Order", qikink_order.name)

	@property
	def listed_ids(self) -> list[list[str]]:
		"""The order ids that each order-list call asked for."""
		return [request.params["ids"].split(",") for request in self.qikink.get_requests(ORDER_LIST_PATH)]


class TestQikinkSyncRecords(QikinkSyncTestCase):
	def test_the_sync_records_the_shipment_qikink_reports(self):
		qikink_order = self.place_pushed_order()
		self.qikink.report(qikink_order.qikink_order_id, "In-Transit", **SHIPPED)

		started_on = now_datetime()

		self.run_sync()

		qikink_order.reload()
		self.assertEqual(
			(qikink_order.qikink_status, qikink_order.awb, qikink_order.courier, qikink_order.tracking_link),
			("In-Transit", AWB, "Bluedart Surface", TRACKING_LINK),
		)
		self.assertTrue(started_on <= qikink_order.last_synced_on <= now_datetime())


class TestQikinkTrackingEvents(QikinkSyncTestCase):
	def test_a_status_change_adds_a_tracking_event_at_the_sync_time(self):
		qikink_order = self.place_pushed_order()
		self.qikink.report(qikink_order.qikink_order_id, "Printed")

		self.run_sync()

		qikink_order.reload()
		(event,) = qikink_order.tracking_events
		self.assertEqual((event.status, event.timestamp), ("Printed", qikink_order.last_synced_on))

	def test_only_a_change_of_the_qikink_status_adds_an_event(self):
		qikink_order = self.place_pushed_order()
		self.run_sync()
		self.qikink.report(qikink_order.qikink_order_id, "Live", **SHIPPED)
		self.run_sync()
		self.qikink.report(qikink_order.qikink_order_id, "Printed", **SHIPPED)
		self.run_sync()
		self.run_sync()

		qikink_order.reload()
		self.assertEqual([event.status for event in qikink_order.tracking_events], ["Live", "Printed"])


class TestQikinkStatusMap(QikinkSyncTestCase):
	def test_each_qikink_status_sets_the_state_of_the_spec(self):
		qikink_order = self.place_pushed_order()

		for statuses, state in ((NORMAL_STATUSES, "Pushed"), (PROBLEM_STATUSES, "Needs Attention")):
			for status in statuses:
				with self.subTest(status=status):
					qikink_order = self.sync_status(qikink_order, status)
					self.assertEqual((qikink_order.qikink_status, qikink_order.status), (status, state))

	def test_a_final_status_completes_the_order(self):
		for status in ("Delivered", "Self collect", "Returned", "Partially Returned"):
			with self.subTest(status=status):
				qikink_order = self.sync_status(self.place_pushed_order(), status)
				self.assertEqual((qikink_order.qikink_status, qikink_order.status), (status, "Completed"))

	def test_the_map_ignores_case(self):
		qikink_order = self.sync_status(self.place_pushed_order(), "ON HOLD")
		self.assertEqual(qikink_order.status, "Needs Attention")

		qikink_order = self.sync_status(qikink_order, "Picked Up")

		self.assertEqual((qikink_order.qikink_status, qikink_order.status), ("Picked Up", "Pushed"))

	def test_an_unknown_status_changes_nothing_and_is_logged(self):
		qikink_order = self.sync_status(self.place_pushed_order(), "Live")
		error_filters = {"reference_name": qikink_order.name, "method": "Unknown Qikink status"}
		errors_before = frappe.db.count("Error Log", error_filters)

		synced_order = self.sync_status(qikink_order, "Archived", **SHIPPED)

		self.assertEqual(synced_order.as_dict(), qikink_order.as_dict())
		self.assertEqual(frappe.db.count("Error Log", error_filters), errors_before + 1)
		self.assertIn("Archived", frappe.get_last_doc("Error Log", error_filters).error)


class TestQikinkDelivery(QikinkSyncTestCase):
	def get_delivered_quantities(self, qikink_order) -> dict:
		"""The delivered and ordered quantity of each line, on the Purchase Order and the Sales Order."""
		purchase_order = frappe.get_doc("Purchase Order", qikink_order.purchase_order)
		sales_order = frappe.get_doc("Sales Order", qikink_order.sales_order)
		return {
			"purchase_order": [(row.item_code, row.received_qty, row.qty) for row in purchase_order.items],
			"sales_order": [(row.item_code, row.delivered_qty, row.qty) for row in sales_order.items],
			"per_delivered": round(sales_order.per_delivered, 2),
		}

	def test_a_delivery_delivers_each_purchase_order_line_and_the_sales_order_shows_it(self):
		for status in ("Delivered", "Self collect"):
			with self.subTest(status=status):
				qikink_order = self.sync_status(self.place_pushed_order(), status)

				self.assertEqual(
					self.get_delivered_quantities(qikink_order),
					{
						"purchase_order": [(self.variant, 1, 1)],
						# The warehouse line waits for its own Delivery Note.
						"sales_order": [(self.variant, 1, 1), (self.warehouse_item, 0, 2)],
						"per_delivered": 33.33,
					},
				)

	def test_a_delivery_event_takes_the_time_qikink_delivered(self):
		qikink_order = self.sync_status(
			self.place_pushed_order(), "Delivered", delivered_on="2026-10-01 17:20:16"
		)

		self.assertEqual(
			[(event.status, event.timestamp) for event in qikink_order.tracking_events],
			[("Delivered", get_datetime("2026-10-01 17:20:16"))],
		)

	def test_a_return_leaves_the_purchase_order_as_it_is(self):
		for status in ("Returned", "Partially Returned"):
			with self.subTest(status=status):
				qikink_order = self.sync_status(self.place_pushed_order(), status)

				self.assertEqual(
					self.get_delivered_quantities(qikink_order),
					{
						"purchase_order": [(self.variant, 0, 1)],
						"sales_order": [(self.variant, 0, 1), (self.warehouse_item, 0, 2)],
						"per_delivered": 0,
					},
				)


class TestQikinkProblemAlerts(QikinkSyncTestCase):
	def test_a_problem_alerts_each_notify_user_once(self):
		qikink_order = self.sync_status(self.place_pushed_order(), "Incorrect/incomplete Address")

		self.assertEqual(qikink_order.status, "Needs Attention")
		self.assertEqual(
			self.get_alerts(qikink_order), {"desk": self.notify_users, "email": self.notify_users}
		)
		notification = frappe.get_last_doc("Notification Log", {"document_name": qikink_order.name})
		self.assertIn(qikink_order.order_number, notification.subject)
		self.assertIn("Incorrect/incomplete Address", notification.email_content)

	def test_a_problem_that_stays_makes_no_new_alert(self):
		qikink_order = self.sync_status(self.place_pushed_order(), "On Hold")

		self.run_sync()
		self.sync_status(qikink_order, "Exception")

		self.assertEqual(
			self.get_alerts(qikink_order), {"desk": self.notify_users, "email": self.notify_users}
		)

	def test_a_new_problem_after_a_normal_status_alerts_again(self):
		qikink_order = self.sync_status(self.place_pushed_order(), "On Hold")
		qikink_order = self.sync_status(qikink_order, "In-Transit")
		self.assertEqual(qikink_order.status, "Pushed")

		qikink_order = self.sync_status(qikink_order, "RTO Initiated")

		self.assertEqual(qikink_order.status, "Needs Attention")
		self.assertEqual(self.get_alerts(qikink_order)["desk"], sorted(self.notify_users * 2))

	def test_a_problem_on_an_order_that_fails_to_save_sends_no_alert(self):
		"""The next run tries the order again, so an alert now would repeat on each run."""
		qikink_order = self.place_pushed_order()
		# Longer than the 140 characters of the Tracking Link field.
		self.qikink.report(
			qikink_order.qikink_order_id, "On Hold", tracking_link=f"{TRACKING_LINK}&{'x' * 140}"
		)

		with patch("frappe.enqueue") as enqueue:
			self.run_sync()

		self.assertEqual(frappe.db.get_value("Qikink Order", qikink_order.name, "status"), "Pushed")
		self.assertEqual(
			[call for call in enqueue.call_args_list if call.args[0].endswith("make_notification_logs")], []
		)


class TestQikinkSyncRun(QikinkSyncTestCase):
	def count_errors(self, qikink_order, title: str) -> int:
		# Error Logs outlive the rollback, and a later run reuses the Qikink Order name.
		return frappe.db.count("Error Log", {"reference_name": qikink_order.name, "method": title})

	def test_the_sync_reads_only_the_pushed_and_needs_attention_orders(self):
		pushed_order = self.place_pushed_order()
		problem_order = self.place_pushed_order()
		self.qikink.report(problem_order.qikink_order_id, "On Hold")
		delivered_order = self.place_pushed_order()
		self.qikink.report(delivered_order.qikink_order_id, "Delivered")
		self.run_sync()
		self.refuse_next_create()
		self.push(self.place_prepaid_order())
		self.qikink.requests.clear()

		self.run_sync()

		self.assertEqual(self.listed_ids, [[pushed_order.qikink_order_id, problem_order.qikink_order_id]])

	def test_the_sync_asks_for_10_orders_in_each_call(self):
		qikink_orders = [self.place_pushed_order() for _ in range(11)]

		self.run_sync()

		order_ids = [qikink_order.qikink_order_id for qikink_order in qikink_orders]
		self.assertEqual(self.listed_ids, [order_ids[:10], order_ids[10:]])
		self.assertEqual(
			frappe.get_all(
				"Qikink Order",
				filters={"name": ["in", [qikink_order.name for qikink_order in qikink_orders]]},
				pluck="qikink_status",
			),
			["Live"] * 11,
		)

	def test_a_page_that_fails_does_not_stop_the_next_page(self):
		qikink_orders = [self.place_pushed_order() for _ in range(11)]
		self.qikink.list_replies = [make_reply(503, {})]
		errors_before = frappe.db.count("Error Log", {"method": "Qikink order list failed"})

		self.run_sync()

		self.assertEqual(
			frappe.db.count("Error Log", {"method": "Qikink order list failed"}), errors_before + 1
		)
		self.assertEqual(
			[
				frappe.db.get_value("Qikink Order", qikink_order.name, "qikink_status")
				for qikink_order in qikink_orders
			],
			[None] * 10 + ["Live"],
		)

	def test_an_order_that_breaks_the_sync_does_not_stop_the_others(self):
		broken_order = self.place_pushed_order()
		next_order = self.place_pushed_order()
		self.qikink.report(broken_order.qikink_order_id, "Delivered", delivered_on="soon", **SHIPPED)
		self.qikink.report(next_order.qikink_order_id, "Delivered")
		errors_before = self.count_errors(broken_order, "Qikink sync failed")

		self.run_sync()

		self.assertEqual(frappe.get_doc("Qikink Order", broken_order.name).as_dict(), broken_order.as_dict())
		self.assertEqual(self.count_errors(broken_order, "Qikink sync failed"), errors_before + 1)
		self.assertEqual(frappe.db.get_value("Qikink Order", next_order.name, "status"), "Completed")

	def test_an_order_qikink_does_not_list_is_logged(self):
		qikink_order = self.place_pushed_order()
		self.qikink.orders.clear()
		errors_before = self.count_errors(qikink_order, "Qikink sync failed")

		self.run_sync()

		self.assertEqual(frappe.get_doc("Qikink Order", qikink_order.name).as_dict(), qikink_order.as_dict())
		self.assertEqual(self.count_errors(qikink_order, "Qikink sync failed"), errors_before + 1)
		self.assertIn(
			qikink_order.qikink_order_id,
			frappe.get_last_doc("Error Log", {"reference_name": qikink_order.name}).error,
		)

	def test_the_sandbox_sync_pushes_but_reads_nothing(self):
		configure_qikink(self, supplier=self.supplier)
		pushed_order = self.place_pushed_order()
		waiting_order = self.push_into_timeouts(self.place_prepaid_order())

		self.run_sync()

		self.assertEqual(self.qikink.get_requests(ORDER_LIST_PATH), [])
		self.assertEqual(frappe.db.get_value("Qikink Order", waiting_order.name, "status"), "Pushed")
		self.assertIsNone(frappe.db.get_value("Qikink Order", pushed_order.name, "qikink_status"))


class TestQikinkSyncNow(QikinkSyncTestCase):
	"""Staff click "Sync now" on the Qikink Order."""

	def click_sync_now(self, qikink_order):
		self.click("sync", qikink_order)

	def test_the_form_offers_sync_now_while_qikink_ships_the_order(self):
		qikink_order = self.get_qikink_order(self.place_prepaid_order())
		offers = {}
		for status in ("Queued", "Pushed", "Failed", "Needs Attention", "Completed", "Cancelled"):
			frappe.db.set_value("Qikink Order", qikink_order.name, "status", status)
			offers[status] = self.get_form_onload("Qikink Order", qikink_order.name)["is_syncable"]

		self.assertEqual(
			offers,
			{
				"Queued": False,
				"Pushed": True,
				"Failed": False,
				"Needs Attention": True,
				"Completed": False,
				"Cancelled": False,
			},
		)

	def test_sync_now_reads_the_one_order_and_delivers_it(self):
		"""A Sales Manager clicks it, who may not change Purchase Orders."""
		self.place_pushed_order()
		qikink_order = self.place_pushed_order()
		self.qikink.report(qikink_order.qikink_order_id, "Delivered", **SHIPPED)
		frappe.set_user(self.notify_users[0])

		self.click_sync_now(qikink_order)

		frappe.set_user("Administrator")
		qikink_order.reload()
		self.assertEqual((qikink_order.status, qikink_order.awb), ("Completed", AWB))
		self.assertEqual(self.listed_ids, [[qikink_order.qikink_order_id]])
		purchase_order = frappe.get_doc("Purchase Order", qikink_order.purchase_order)
		self.assertEqual([(row.received_qty, row.qty) for row in purchase_order.items], [(1, 1)])

	def test_sync_now_refuses_an_order_qikink_no_longer_ships(self):
		qikink_order = self.place_pushed_order()
		self.qikink.report(qikink_order.qikink_order_id, "Delivered")
		self.click_sync_now(qikink_order)
		self.qikink.requests.clear()

		with self.assertRaisesRegex(frappe.ValidationError, "Only a Pushed or Needs Attention"):
			self.click_sync_now(qikink_order)
		self.assertEqual(self.qikink.requests, [])

	def test_sync_now_in_the_sandbox_says_why_it_cannot_read(self):
		configure_qikink(self, supplier=self.supplier)
		qikink_order = self.place_pushed_order()

		with self.assertRaisesRegex(frappe.ValidationError, "sandbox has no order list"):
			self.click_sync_now(qikink_order)
		self.assertEqual(self.qikink.get_requests(ORDER_LIST_PATH), [])

	def test_sync_now_while_qikink_is_down_says_why(self):
		qikink_order = self.place_pushed_order()
		self.qikink.list_replies = [requests.Timeout("Read timed out")]

		with self.assertRaisesRegex(frappe.ValidationError, "Qikink did not answer: Timeout"):
			self.click_sync_now(qikink_order)

	def test_sync_now_needs_write_permission(self):
		qikink_order = self.place_pushed_order()
		reader = create_staff("Sales User")
		frappe.share.add("Qikink Order", qikink_order.name, reader, read=1, notify=0)
		frappe.set_user(reader)

		with self.assertRaises(frappe.PermissionError):
			self.click_sync_now(qikink_order)
		self.assertEqual(self.qikink.get_requests(ORDER_LIST_PATH), [])
