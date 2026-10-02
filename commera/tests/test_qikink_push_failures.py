# Copyright (c) 2026, company@bwhstudios.com and Contributors

from unittest.mock import patch

import frappe
import requests
from frappe.handler import run_doc_method
from frappe.utils import set_request

from commera.qikink.jobs import queue_qikink_sync, sync_qikink_orders
from commera.tests import create_staff
from commera.tests.qikink import (
	CREATE_ORDER_PATH,
	ORDER_LIST_PATH,
	QIKINK_ORDER_ID,
	TOKEN_PATH,
	configure_qikink,
	make_reply,
)
from commera.tests.test_qikink_push import PUSH_JOB, QIKINK_SKU, QikinkPushTestCase

QIKINK_ERROR = "Missing field: phone"


class QikinkFailureTestCase(QikinkPushTestCase):
	"""Two staff members get the Qikink alerts."""

	def setUp(self):
		super().setUp()
		self.notify_users = sorted(create_staff("Sales Manager") for _ in range(2))
		configure_qikink(
			self, supplier=self.supplier, notify_users=[{"user": user} for user in self.notify_users]
		)
		self.create_outgoing_email_account()

	def create_outgoing_email_account(self):
		# Frappe keeps the outgoing account in frappe.local, past the rollback of this one.
		self.addCleanup(setattr, frappe.local, "outgoing_email_account", {})
		frappe.get_doc(
			{
				"doctype": "Email Account",
				"email_account_name": "ZZ Qikink Alerts",
				"email_id": "zz-qikink-alerts@example.com",
				"enable_outgoing": 1,
				"default_outgoing": 1,
				"smtp_server": "localhost",
			}
		).insert(ignore_permissions=True)

	def run_sync(self):
		# The job commits each order, and inside a test that commit would escape the rollback.
		with patch.object(frappe.db, "commit"):
			sync_qikink_orders()

	def click(self, method: str, qikink_order):
		"""The call a form button makes: a POST to run the whitelisted method."""
		self.addCleanup(setattr, frappe.local, "request", getattr(frappe.local, "request", None))
		set_request(method="POST")
		frappe.local.response = frappe._dict(docs=[])
		run_doc_method(method, dt="Qikink Order", dn=qikink_order.name)

	def refuse_next_create(self, error: str = QIKINK_ERROR):
		self.qikink.create_replies.append(make_reply(400, {"error": error, "status_code": "400"}))

	def push_into_timeouts(self, sales_order, times: int = 1):
		"""Push into a timeout each time, and hand back the Qikink Order."""
		self.qikink.create_replies = [requests.Timeout("Read timed out")] * times
		for _ in range(times):
			qikink_order = self.push(sales_order)
		return qikink_order

	def get_alerts(self, qikink_order) -> dict[str, list[str]]:
		"""Who got a Desk notification, and who got an email, about the Qikink Order."""
		reference = {"document_type": "Qikink Order", "document_name": qikink_order.name}
		emails = frappe.get_all(
			"Email Queue",
			filters={"reference_doctype": "Qikink Order", "reference_name": qikink_order.name},
			pluck="name",
		)
		return {
			"desk": sorted(frappe.get_all("Notification Log", filters=reference, pluck="for_user")),
			"email": sorted(
				frappe.get_all("Email Queue Recipient", filters={"parent": ["in", emails]}, pluck="recipient")
			),
		}

	def assert_failed(self, qikink_order, error: str):
		"""The push stopped with the error, and each of the Notify Users heard about it once."""
		self.assertEqual(qikink_order.status, "Failed")
		self.assertIn(error, qikink_order.error)
		self.assertEqual(
			self.get_alerts(qikink_order), {"desk": self.notify_users, "email": self.notify_users}
		)


class TestQikinkAlerts(QikinkFailureTestCase):
	def test_a_failure_tells_each_notify_user_in_desk_and_by_email_why(self):
		self.refuse_next_create()

		qikink_order = self.push(self.place_prepaid_order())

		self.assert_failed(qikink_order, QIKINK_ERROR)
		notification = frappe.get_last_doc("Notification Log", {"document_name": qikink_order.name})
		self.assertIn(qikink_order.order_number, notification.subject)
		self.assertIn(QIKINK_ERROR, notification.email_content)
		email = frappe.get_last_doc("Email Queue", {"reference_name": qikink_order.name})
		self.assertIn(QIKINK_ERROR, email.message)

	def test_a_site_without_outgoing_email_still_records_the_failure(self):
		frappe.delete_doc("Email Account", "ZZ Qikink Alerts", ignore_permissions=True)
		self.refuse_next_create()

		qikink_order = self.push(self.place_prepaid_order())

		self.assertEqual(qikink_order.status, "Failed")
		self.assertEqual(self.get_alerts(qikink_order), {"desk": self.notify_users, "email": []})


class TestQikinkRefusal(QikinkFailureTestCase):
	def test_a_400_reply_sets_failed_with_the_qikink_error(self):
		self.refuse_next_create()

		qikink_order = self.push(self.place_prepaid_order())

		self.assert_failed(qikink_order, QIKINK_ERROR)
		self.assertEqual(len(self.qikink.get_requests(CREATE_ORDER_PATH)), 1)

	def test_a_401_reply_gets_a_new_token_and_repeats_the_call(self):
		self.qikink.create_replies = [make_reply(401, {"error": "Invalid Accesstoken"})]

		qikink_order = self.push(self.place_prepaid_order())

		self.assertEqual(qikink_order.status, "Pushed")
		self.assertEqual(len(self.qikink.get_requests(TOKEN_PATH)), 2)
		self.assertEqual(
			[request.headers["Accesstoken"] for request in self.qikink.get_requests(CREATE_ORDER_PATH)],
			["zz-access-token-1", "zz-access-token-2"],
		)

	def test_a_401_reply_to_the_new_token_sets_failed(self):
		self.qikink.create_replies = [make_reply(401, {"error": "Invalid Accesstoken"})] * 2

		qikink_order = self.push(self.place_prepaid_order())

		self.assert_failed(qikink_order, "Invalid Accesstoken")
		self.assertEqual(len(self.qikink.get_requests(CREATE_ORDER_PATH)), 2)


class TestQikinkOrderCheck(QikinkFailureTestCase):
	"""Commera checks the order before it calls Qikink, and names what staff must fix."""

	def setUp(self):
		super().setUp()
		self.sales_order = self.place_prepaid_order()

	def set_shipping_address(self, **values):
		frappe.db.set_value("Address", self.sales_order.shipping_address_name, values)

	def assert_refused_before_qikink(self, *errors: str):
		qikink_order = self.push(self.sales_order)

		for error in errors:
			self.assert_failed(qikink_order, error)
		self.assertEqual(self.qikink.get_requests(CREATE_ORDER_PATH), [])

	def test_a_missing_state(self):
		self.set_shipping_address(state=None)

		self.assert_refused_before_qikink("Set the state of the shipping address")

	def test_an_indian_state_outside_the_list(self):
		self.set_shipping_address(state="Tamilnadu")

		self.assert_refused_before_qikink("Tamilnadu is not an Indian state")

	def test_an_order_without_a_shipping_address(self):
		frappe.db.set_value(
			"Sales Order", self.sales_order.name, {"shipping_address_name": None, "customer_address": None}
		)

		self.assert_refused_before_qikink(f"Set the shipping address of {self.sales_order.name}")

	def test_a_pincode_that_is_not_numeric(self):
		self.set_shipping_address(pincode="6000O1")

		self.assert_refused_before_qikink("pincode of digits only")

	def test_a_pincode_of_spaces_only(self):
		self.set_shipping_address(pincode="   ")

		self.assert_refused_before_qikink("pincode of digits only")

	def test_a_missing_phone(self):
		self.set_shipping_address(phone=None)
		frappe.db.set_value("Sales Order", self.sales_order.name, "contact_mobile", None)

		self.assert_refused_before_qikink("Set a phone number")

	def test_a_missing_email(self):
		self.set_shipping_address(email_id=None)
		frappe.db.set_value("Sales Order", self.sales_order.name, "contact_email", None)

		self.assert_refused_before_qikink("Set an email address")

	def test_a_country_without_a_country_code(self):
		frappe.db.set_value("Country", "India", "code", None)

		self.assert_refused_before_qikink("Set the code of the country India")

	def test_a_missing_qikink_sku(self):
		frappe.db.set_value("Item", self.variant, "custom_qikink_sku", None)

		self.assert_refused_before_qikink(f"Set the Qikink SKU of {self.variant}")

	def test_a_quantity_of_more_than_100(self):
		qikink_line = self.get_line(self.sales_order, self.variant)
		frappe.db.set_value("Sales Order Item", qikink_line.name, {"qty": 101, "stock_qty": 101})

		self.assert_refused_before_qikink(f"Qikink takes at most 100 of {self.variant}")

	def test_the_message_names_each_problem(self):
		self.set_shipping_address(state=None)
		frappe.db.set_value("Item", self.variant, "custom_qikink_sku", None)

		self.assert_refused_before_qikink("Set the state", "Set the Qikink SKU")


class TestQikinkPurchaseOrderRefusal(QikinkFailureTestCase):
	def test_a_purchase_order_erpnext_refuses_sets_failed_and_leaves_no_purchase_order(self):
		"""The approval rule refuses the submit after ERPNext saved the Purchase Order."""
		sales_order = self.place_prepaid_order()
		frappe.get_doc(
			{
				"doctype": "Authorization Rule",
				"transaction": "Purchase Order",
				"based_on": "Grand Total",
				"system_user": "Administrator",
				"approving_user": self.notify_users[0],
				"value": 0,
			}
		).insert(ignore_permissions=True)

		qikink_order = self.push(sales_order)

		self.assert_failed(qikink_order, "Can be approved by")
		self.assertIsNone(qikink_order.purchase_order)
		self.assertFalse(frappe.db.exists("Purchase Order Item", {"sales_order": sales_order.name}))
		qikink_line = self.get_line(frappe.get_doc("Sales Order", sales_order.name), self.variant)
		self.assertEqual(qikink_line.ordered_qty, 0)
		self.assertEqual(self.qikink.requests, [])


class TestQikinkRetry(QikinkFailureTestCase):
	def assert_no_alert(self, qikink_order):
		self.assertEqual(self.get_alerts(qikink_order), {"desk": [], "email": []})

	def test_a_timeout_adds_an_attempt_and_leaves_the_order_queued(self):
		qikink_order = self.push_into_timeouts(self.place_prepaid_order())

		self.assertEqual((qikink_order.status, qikink_order.attempts), ("Queued", 1))
		self.assertIn("Timeout", qikink_order.error)
		self.assert_no_alert(qikink_order)

	def test_a_connection_error_a_429_and_a_5xx_each_add_an_attempt(self):
		sales_order = self.place_prepaid_order()
		self.qikink.create_replies = [
			requests.ConnectionError("Connection refused"),
			make_reply(429, {}),
			make_reply(503, {}),
		]

		pushes = [self.push(sales_order) for _ in range(3)]

		self.assertEqual(
			[(qikink_order.status, qikink_order.attempts) for qikink_order in pushes],
			[("Queued", 1), ("Queued", 2), ("Failed", 3)],
		)

	def test_a_dropped_or_unreadable_reply_adds_an_attempt(self):
		"""Qikink may have made the order, so the order waits for a retry, which looks it up first."""
		sales_order = self.place_prepaid_order()
		self.qikink.create_replies = [
			requests.exceptions.ChunkedEncodingError("Connection broken"),
			make_reply(200, "<html>Bad gateway</html>"),
			make_reply(200, {"message": "Order created successfully"}),
		]

		pushes = [self.push(sales_order) for _ in range(3)]

		self.assertEqual(
			[(qikink_order.status, qikink_order.attempts) for qikink_order in pushes],
			[("Queued", 1), ("Queued", 2), ("Failed", 3)],
		)
		self.assertIn("no order id", pushes[-1].error)

	def test_the_third_timeout_sets_failed(self):
		qikink_order = self.push_into_timeouts(self.place_prepaid_order(), times=3)

		self.assertEqual(qikink_order.attempts, 3)
		self.assert_failed(qikink_order, "Timeout")
		self.assertEqual(len(self.qikink.get_requests(CREATE_ORDER_PATH)), 3)

	def test_a_live_retry_finds_the_order_qikink_took_and_makes_no_second_one(self):
		configure_qikink(self, supplier=self.supplier, sandbox=0)
		sales_order = self.place_prepaid_order()
		qikink_order = self.push_into_timeouts(sales_order)
		# The create reached Qikink, but its reply did not reach Commera.
		self.qikink.add_order(qikink_order.order_number, order_id=QIKINK_ORDER_ID + 1)

		qikink_order = self.push(sales_order)

		self.assertEqual(
			(qikink_order.status, qikink_order.qikink_order_id), ("Pushed", str(QIKINK_ORDER_ID + 1))
		)
		self.assertEqual(len(self.qikink.get_requests(CREATE_ORDER_PATH)), 1)
		self.assertEqual(
			frappe.get_all("Purchase Order Item", {"sales_order": sales_order.name}, pluck="parent"),
			[qikink_order.purchase_order],
		)

	def test_a_live_retry_sends_the_order_qikink_does_not_have(self):
		configure_qikink(self, supplier=self.supplier, sandbox=0)
		sales_order = self.place_prepaid_order()
		self.push_into_timeouts(sales_order)

		qikink_order = self.push(sales_order)

		self.assertEqual((qikink_order.status, qikink_order.error), ("Pushed", None))
		(lookup,) = self.qikink.get_requests(ORDER_LIST_PATH)
		self.assertEqual(lookup.params, {"order_reference_no": qikink_order.order_number})
		self.assertEqual(len(self.qikink.get_requests(CREATE_ORDER_PATH)), 2)

	def test_a_sandbox_retry_skips_the_lookup(self):
		sales_order = self.place_prepaid_order()
		self.push_into_timeouts(sales_order)

		qikink_order = self.push(sales_order)

		self.assertEqual(qikink_order.status, "Pushed")
		self.assertEqual(self.qikink.get_requests(ORDER_LIST_PATH), [])
		self.assertEqual(len(self.qikink.get_requests(CREATE_ORDER_PATH)), 2)


class TestQikinkPushState(QikinkFailureTestCase):
	def test_a_push_reads_the_state_again_and_stops_on_a_cancelled_order(self):
		"""A cancel that lands while the push waits for the row lock stops the push."""
		sales_order = self.place_prepaid_order()
		qikink_order = self.get_qikink_order(sales_order)
		frappe.db.set_value("Qikink Order", qikink_order.name, "status", "Cancelled")

		qikink_order.push()

		self.assertEqual(frappe.db.get_value("Qikink Order", qikink_order.name, "status"), "Cancelled")
		self.assertFalse(frappe.db.exists("Purchase Order Item", {"sales_order": sales_order.name}))
		self.assertEqual(self.qikink.requests, [])

	def test_a_failed_order_goes_through_once_staff_fix_the_data(self):
		sales_order = self.place_prepaid_order()
		frappe.db.set_value("Item", self.variant, "custom_qikink_sku", None)
		self.assertEqual(self.push(sales_order).status, "Failed")
		frappe.db.set_value("Item", self.variant, "custom_qikink_sku", QIKINK_SKU)

		qikink_order = self.push(sales_order)

		self.assertEqual((qikink_order.status, qikink_order.error), ("Pushed", None))
		self.assertEqual(len(self.qikink.get_requests(CREATE_ORDER_PATH)), 1)


class TestQikinkPushButton(QikinkFailureTestCase):
	"""Staff click "Push to Qikink" on the Qikink Order or on the Sales Order."""

	def click_push_to_qikink(self, qikink_order):
		self.click("requeue_push", qikink_order)

	def test_the_forms_offer_push_to_qikink_while_the_order_is_queued_or_failed(self):
		sales_order = self.place_prepaid_order()
		qikink_order = self.get_qikink_order(sales_order)
		offers = {}
		for status in ("Queued", "Pushed", "Failed", "Needs Attention", "Completed", "Cancelled"):
			frappe.db.set_value("Qikink Order", qikink_order.name, "status", status)
			sales_order_form = self.get_form_onload("Sales Order", sales_order.name)
			qikink_order_form = self.get_form_onload("Qikink Order", qikink_order.name)
			offers[status] = (
				sales_order_form["qikink_order"]["is_pushable"],
				qikink_order_form["is_pushable"],
			)

		# Each pair: the Sales Order form, then the Qikink Order form.
		self.assertEqual(
			offers,
			{
				"Queued": (True, True),
				"Pushed": (False, False),
				"Failed": (True, True),
				"Needs Attention": (False, False),
				"Completed": (False, False),
				"Cancelled": (False, False),
			},
		)

	def test_push_to_qikink_starts_the_attempts_over_and_queues_the_push(self):
		sales_order = self.place_prepaid_order()
		qikink_order = self.push_into_timeouts(sales_order, times=3)
		frappe.set_user(self.notify_users[0])

		with patch("frappe.enqueue") as enqueue:
			self.click_push_to_qikink(qikink_order)

		qikink_order.reload()
		self.assertEqual((qikink_order.status, qikink_order.attempts), ("Queued", 0))
		self.assertEqual(
			[call.kwargs["qikink_order"] for call in enqueue.call_args_list if call.args == (PUSH_JOB,)],
			[qikink_order.name],
		)
		frappe.set_user("Administrator")
		self.assertEqual(self.push(sales_order).status, "Pushed")

	def test_push_to_qikink_refuses_an_order_qikink_has(self):
		qikink_order = self.push(self.place_prepaid_order())

		with self.assertRaises(frappe.ValidationError):
			self.click_push_to_qikink(qikink_order)


class TestQikinkSyncJob(QikinkFailureTestCase):
	def get_status_and_attempts(self, qikink_order) -> tuple[str, int]:
		return frappe.db.get_value("Qikink Order", qikink_order.name, ["status", "attempts"])

	def test_the_sync_pushes_an_order_that_waits_for_its_retry(self):
		qikink_order = self.push_into_timeouts(self.place_prepaid_order())

		self.run_sync()

		self.assertEqual(self.get_status_and_attempts(qikink_order), ("Pushed", 1))

	def test_the_sync_leaves_failed_orders_and_orders_with_3_attempts_alone(self):
		self.refuse_next_create()
		failed_order = self.push(self.place_prepaid_order())
		given_up_order = self.get_qikink_order(self.place_prepaid_order())
		frappe.db.set_value("Qikink Order", given_up_order.name, "attempts", 3)

		self.run_sync()

		self.assertEqual(self.get_status_and_attempts(failed_order), ("Failed", 0))
		self.assertEqual(self.get_status_and_attempts(given_up_order), ("Queued", 3))
		self.assertEqual(len(self.qikink.get_requests(CREATE_ORDER_PATH)), 1)

	def test_the_sync_pushes_nothing_while_qikink_is_off(self):
		qikink_order = self.push_into_timeouts(self.place_prepaid_order())
		configure_qikink(self, supplier=self.supplier, enabled=0)

		self.run_sync()

		self.assertEqual(self.get_status_and_attempts(qikink_order), ("Queued", 1))
		self.assertEqual(len(self.qikink.get_requests(CREATE_ORDER_PATH)), 1)

	def test_an_order_that_breaks_the_sync_does_not_stop_the_others(self):
		broken_order = self.push_into_timeouts(self.place_prepaid_order())
		next_order = self.get_qikink_order(self.place_prepaid_order())
		self.qikink.create_replies = [RuntimeError("The transport broke")]
		# Error Logs outlive the rollback, and a later run reuses the Qikink Order name.
		error_filters = {"reference_name": broken_order.name, "method": "Qikink push failed"}
		errors_before = frappe.db.count("Error Log", error_filters)

		self.run_sync()

		self.assertEqual(self.get_status_and_attempts(broken_order), ("Queued", 1))
		self.assertEqual(self.get_status_and_attempts(next_order), ("Pushed", 0))
		self.assertEqual(frappe.db.count("Error Log", error_filters), errors_before + 1)

	def test_the_sync_runs_every_30_minutes_on_the_long_queue(self):
		cron_jobs = frappe.get_hooks("scheduler_events")["cron"]["*/30 * * * *"]
		self.assertIn(f"{queue_qikink_sync.__module__}.{queue_qikink_sync.__name__}", cron_jobs)

		with patch("frappe.enqueue") as enqueue:
			queue_qikink_sync()

		(call,) = enqueue.call_args_list
		self.assertEqual(
			(call.args, call.kwargs["queue"]),
			((f"{sync_qikink_orders.__module__}.{sync_qikink_orders.__name__}",), "long"),
		)
