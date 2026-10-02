# Copyright (c) 2026, company@bwhstudios.com and Contributors

import json
import re
from unittest.mock import patch

import frappe
import requests
from frappe.desk.form.load import getdoc
from frappe.utils import getdate

from commera.install import TEST_COMPANY, TEST_CURRENCY
from commera.qikink.jobs import push_qikink_order
from commera.tests import create_staff
from commera.tests.qikink import (
	CLIENT_SECRET,
	CREATE_ORDER_PATH,
	QIKINK_ORDER_ID,
	SETTINGS,
	TOKEN_PATH,
	FakeQikink,
	configure_qikink,
	make_reply,
)
from commera.tests.test_drop_ship import ITEM_RATE, DropShipCheckoutTestCase

QIKINK_SKU = "ZZ-QK-TEE-M"
# Exactly the 90 characters Qikink takes in address1.
ADDRESS1 = "Flat 12 Sunrise Apartments, 45 Fourth Cross Street, Gandhi Nagar Extension, Velachery Main"
BUYING_RATE = 180.0
PUSH_JOB = f"{push_qikink_order.__module__}.{push_qikink_order.__name__}"


class QikinkPushTestCase(DropShipCheckoutTestCase):
	"""A shopper buys a Qikink variant and a warehouse item in one cart."""

	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		frappe.db.set_value("Item", self.variant, "custom_qikink_sku", QIKINK_SKU)
		configure_qikink(self, supplier=self.supplier)
		self.qikink = FakeQikink()
		transport = patch("commera.qikink.client.get_request_session", return_value=self.qikink)
		transport.start()
		self.addCleanup(transport.stop)

	def get_billing_address(self) -> dict:
		return {
			**super().get_billing_address(),
			"full_address": f"{ADDRESS1} Road, Block B",
			"landmark": "Near the park",
			"state": "Tamil Nadu",
			"po_box": "600 001",
			"phone_number": "+91 98400 12345",
		}

	def get_qikink_order(self, sales_order):
		return frappe.get_doc("Qikink Order", {"sales_order": sales_order.name})

	def push(self, sales_order):
		"""Run the push job the Sales Order queued, and hand back its Qikink Order."""
		push_qikink_order(self.get_qikink_order(sales_order).name)
		return self.get_qikink_order(sales_order)

	def submit_desk_order(self, item_code: str, **line_values):
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"ZZ Qikink Customer {frappe.generate_hash(length=8)}",
				"customer_type": "Individual",
			}
		).insert(ignore_permissions=True)
		sales_order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer.name,
				"company": TEST_COMPANY,
				"transaction_date": getdate(),
				"delivery_date": getdate(),
				"items": [{"item_code": item_code, "qty": 1, "rate": ITEM_RATE, **line_values}],
			}
		).insert(ignore_permissions=True)
		sales_order.submit()
		return sales_order

	def get_form_onload(self, doctype: str, name: str) -> dict:
		frappe.local.response = frappe._dict(docs=[])
		getdoc(doctype, name)
		return frappe.response.docs[0].get_onload()

	def assert_nothing_goes_to_qikink(self, sales_order):
		self.assertFalse(frappe.db.exists("Qikink Order", {"sales_order": sales_order.name}))
		self.assertFalse(frappe.db.exists("Purchase Order Item", {"sales_order": sales_order.name}))


class TestQikinkOrderQueue(QikinkPushTestCase):
	def test_a_prepaid_order_queues_one_push_after_the_commit(self):
		with patch("frappe.enqueue") as enqueue:
			sales_order = self.place_prepaid_order()

		qikink_order = self.get_qikink_order(sales_order)
		self.assertEqual(qikink_order.status, "Queued")
		push_jobs = [call.kwargs for call in enqueue.call_args_list if call.args == (PUSH_JOB,)]
		self.assertEqual(len(push_jobs), 1)
		self.assertEqual(push_jobs[0]["qikink_order"], qikink_order.name)
		self.assertIn(qikink_order.name, push_jobs[0]["job_id"])
		self.assertTrue(push_jobs[0]["deduplicate"])
		self.assertTrue(push_jobs[0]["enqueue_after_commit"])

	def test_the_order_number_is_the_name_in_lower_case_without_the_hyphen(self):
		qikink_order = self.get_qikink_order(self.place_prepaid_order())

		serial = re.fullmatch(r"QK-(\d{5})", qikink_order.name).group(1)
		self.assertEqual(qikink_order.order_number, f"qk{serial}")

	def test_an_order_without_qikink_lines_makes_no_qikink_order(self):
		self.assert_nothing_goes_to_qikink(self.submit_desk_order(self.warehouse_item))

	def test_a_drop_ship_line_of_another_supplier_is_no_qikink_line(self):
		item_code = self.create_item("DROPSHIP", delivered_by_supplier=1)

		sales_order = self.submit_desk_order(
			item_code, delivered_by_supplier=1, supplier=self.create_supplier()
		)

		self.assert_nothing_goes_to_qikink(sales_order)

	def test_an_order_makes_no_qikink_order_while_qikink_is_off(self):
		configure_qikink(self, supplier=self.supplier, enabled=0)

		self.assert_nothing_goes_to_qikink(self.place_prepaid_order())

	def test_a_sales_order_has_one_qikink_order(self):
		sales_order = self.place_prepaid_order()

		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc({"doctype": "Qikink Order", "sales_order": sales_order.name}).insert()


class TestQikinkPush(QikinkPushTestCase):
	def get_create_body(self) -> dict:
		(create_request,) = self.qikink.get_requests(CREATE_ORDER_PATH)
		return create_request.json

	def test_the_push_makes_a_drop_ship_purchase_order_of_the_qikink_lines(self):
		sales_order = self.place_prepaid_order()

		purchase_order = frappe.get_doc("Purchase Order", self.push(sales_order).purchase_order)

		self.assertEqual((purchase_order.docstatus, purchase_order.supplier), (1, self.supplier))
		self.assertEqual(
			[(row.item_code, row.delivered_by_supplier, row.sales_order) for row in purchase_order.items],
			[(self.variant, 1, sales_order.name)],
		)
		self.assertEqual(purchase_order.shipping_address, sales_order.shipping_address_name)
		self.assertEqual(purchase_order.customer_contact_person, sales_order.contact_person)

	def test_a_second_run_of_the_push_makes_no_second_purchase_order(self):
		sales_order = self.place_prepaid_order()
		qikink_order = self.push(sales_order)

		self.push(sales_order)

		self.assertEqual(
			frappe.get_all("Purchase Order Item", {"sales_order": sales_order.name}, pluck="parent"),
			[qikink_order.purchase_order],
		)
		self.assertEqual(len(self.qikink.get_requests(CREATE_ORDER_PATH)), 1)

	def test_a_queued_order_that_has_its_purchase_order_makes_no_second_one(self):
		sales_order = self.place_prepaid_order()
		qikink_order = self.push(sales_order)
		frappe.db.set_value("Qikink Order", qikink_order.name, "status", "Queued")

		self.push(sales_order)

		self.assertEqual(
			frappe.get_all("Purchase Order Item", {"sales_order": sales_order.name}, pluck="parent"),
			[qikink_order.purchase_order],
		)
		self.assertEqual(len(self.qikink.get_requests(CREATE_ORDER_PATH)), 2)

	def test_a_purchase_order_line_costs_the_qikink_buying_price(self):
		price_list = frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": f"ZZ Qikink Buying {frappe.generate_hash(length=8)}",
				"currency": TEST_CURRENCY,
				"buying": 1,
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": self.variant,
				"price_list": price_list.name,
				"price_list_rate": BUYING_RATE,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value("Supplier", self.supplier, "default_price_list", price_list.name)

		purchase_order = frappe.get_doc(
			"Purchase Order", self.push(self.place_prepaid_order()).purchase_order
		)

		self.assertEqual(purchase_order.items[0].rate, BUYING_RATE)

	def test_a_purchase_order_line_without_a_buying_price_costs_nothing(self):
		purchase_order = frappe.get_doc(
			"Purchase Order", self.push(self.place_prepaid_order()).purchase_order
		)

		self.assertEqual((purchase_order.docstatus, purchase_order.items[0].rate), (1, 0))

	def test_a_200_reply_records_the_qikink_order_id_and_sets_pushed(self):
		qikink_order = self.push(self.place_prepaid_order())

		self.assertEqual(
			(qikink_order.status, qikink_order.qikink_order_id), ("Pushed", str(QIKINK_ORDER_ID))
		)
		self.assertEqual(len(self.qikink.get_requests(CREATE_ORDER_PATH)), 1)

	def test_a_prepaid_order_reaches_qikink_under_its_order_number_for_qikink_to_ship(self):
		qikink_order = self.push(self.place_prepaid_order())

		body = self.get_create_body()
		self.assertEqual(
			(body["order_number"], body["qikink_shipping"], body["gateway"]),
			(qikink_order.order_number, 1, "Prepaid"),
		)

	def test_each_line_names_its_qikink_sku_and_selling_price_and_no_design(self):
		self.push(self.place_prepaid_order())

		self.assertEqual(
			self.get_create_body()["line_items"],
			[{"search_from_my_products": 1, "sku": QIKINK_SKU, "quantity": 1, "price": ITEM_RATE}],
		)

	def test_a_cod_order_reaches_qikink_once_staff_approve_it(self):
		sales_order = self.place_cod_order()
		self.assertFalse(frappe.db.exists("Qikink Order", {"sales_order": sales_order.name}))

		sales_order.submit()
		self.push(sales_order)

		self.assertEqual(self.get_create_body()["gateway"], "COD")

	def test_a_cod_order_a_sales_manager_approves_reaches_qikink(self):
		"""The push job runs as the approver, who may not buy from suppliers."""
		sales_order = self.place_cod_order()
		frappe.set_user(create_staff("Sales User", "Sales Manager"))

		sales_order.submit()
		qikink_order = self.push(sales_order)

		self.assertEqual(qikink_order.status, "Pushed")
		self.assertEqual(frappe.db.get_value("Purchase Order", qikink_order.purchase_order, "docstatus"), 1)

	def test_qikink_collects_its_share_of_the_grand_total(self):
		sales_order = self.place_cod_order(cod_charge=31)
		sales_order.submit()
		self.assertEqual((sales_order.net_total, sales_order.grand_total), (750, 781))

		qikink_order = self.push(sales_order)

		# The Qikink line is 250 of the 750 net, so Qikink collects a third of 781.
		self.assertEqual(self.get_create_body()["total_order_value"], 260.33)
		self.assertEqual(qikink_order.total_order_value, 260.33)

	def test_the_parcel_goes_to_the_customer_address_in_the_qikink_format(self):
		self.assertEqual(len(ADDRESS1), 90)

		self.push(self.place_prepaid_order())

		self.assertEqual(
			self.get_create_body()["shipping_address"],
			{
				# The checkout files the shopper's whole name as the contact's first name.
				"first_name": "ZZ Shopper",
				"last_name": "",
				"address1": ADDRESS1,
				"address2": "Road, Block B, Near the park",
				"phone": "9840012345",
				"email": self.shopper,
				"city": "Chennai",
				"zip": "600001",
				"province": "Tamil Nadu",
				"country_code": "IN",
			},
		)

	def test_the_order_asks_for_box_packing_and_the_brand_logo(self):
		configure_qikink(self, supplier=self.supplier, box_packing=1, brand_logo="/files/zz-brand-logo.png")

		self.push(self.place_prepaid_order())

		body = self.get_create_body()
		self.assertEqual(body["add_ons"], [{"box_packing": 1}])
		self.assertRegex(body["brand_logo"], r"^https?://[^/]+/files/zz-brand-logo\.png$")

	def test_an_order_without_a_brand_logo_sends_0(self):
		self.push(self.place_prepaid_order())

		body = self.get_create_body()
		self.assertEqual((body["add_ons"], body["brand_logo"]), ([{"box_packing": 0}], 0))

	def test_an_order_without_a_contact_ships_to_the_customer_name(self):
		sales_order = self.place_prepaid_order()
		frappe.db.set_value("Sales Order", sales_order.name, "contact_person", None)

		self.push(sales_order)

		address = self.get_create_body()["shipping_address"]
		first_name, last_name = sales_order.customer_name.split(" ", 1)
		self.assertEqual((address["first_name"], address["last_name"]), (first_name, last_name))


class TestQikinkClient(QikinkPushTestCase):
	def test_the_token_comes_from_the_client_id_and_the_client_secret(self):
		self.push(self.place_prepaid_order())

		client_id = frappe.get_cached_doc(SETTINGS).client_id
		(token_request,) = self.qikink.get_requests(TOKEN_PATH)
		self.assertEqual(token_request.data, {"ClientId": client_id, "client_secret": CLIENT_SECRET})
		(create_request,) = self.qikink.get_requests(CREATE_ORDER_PATH)
		self.assertEqual(create_request.headers, {"ClientId": client_id, "Accesstoken": "zz-access-token-1"})

	def test_a_second_push_in_the_token_lifetime_asks_for_no_new_token(self):
		self.push(self.place_prepaid_order())
		self.push(self.place_prepaid_order())

		self.assertEqual(len(self.qikink.get_requests(TOKEN_PATH)), 1)
		self.assertEqual(
			[request.headers["Accesstoken"] for request in self.qikink.get_requests(CREATE_ORDER_PATH)],
			["zz-access-token-1", "zz-access-token-1"],
		)

	def test_the_token_is_kept_until_60_seconds_before_it_expires(self):
		self.qikink.token_expires_in = 60

		self.push(self.place_prepaid_order())
		self.push(self.place_prepaid_order())

		self.assertEqual(len(self.qikink.get_requests(TOKEN_PATH)), 2)

	def test_sandbox_and_live_each_get_their_own_token(self):
		self.push(self.place_prepaid_order())
		client_id = frappe.get_cached_doc(SETTINGS).client_id
		configure_qikink(self, supplier=self.supplier, sandbox=0, client_id=client_id)

		self.push(self.place_prepaid_order())

		self.assertEqual(
			[request.url for request in self.qikink.get_requests(TOKEN_PATH)],
			["https://sandbox.qikink.com/api/token", "https://api.qikink.com/api/token"],
		)

	def test_the_sandbox_switch_picks_the_host(self):
		self.push(self.place_prepaid_order())
		configure_qikink(self, supplier=self.supplier, sandbox=0)
		self.push(self.place_prepaid_order())

		self.assertEqual(
			[request.url for request in self.qikink.get_requests(CREATE_ORDER_PATH)],
			["https://sandbox.qikink.com/api/order/create", "https://api.qikink.com/api/order/create"],
		)

	def test_each_call_gives_up_after_30_seconds(self):
		self.push(self.place_prepaid_order())

		self.assertEqual([request.timeout for request in self.qikink.requests], [30, 30])

	def test_each_call_logs_its_endpoint_and_outcome_and_no_personal_data(self):
		self.push(self.place_prepaid_order())

		logs = frappe.get_all(
			"Integration Request",
			filters={"integration_request_service": "Qikink"},
			fields=["data", "output", "error", "request_headers", "status"],
			order_by="creation asc",
		)
		self.assertEqual(
			[(json.loads(log.data), json.loads(log.output), log.status) for log in logs],
			[
				({"endpoint": TOKEN_PATH}, {"status_code": 200}, "Completed"),
				({"endpoint": CREATE_ORDER_PATH}, {"status_code": 200}, "Completed"),
			],
		)
		logged = frappe.as_json(logs)
		for secret in (
			self.shopper,
			"9840012345",
			ADDRESS1,
			"Near the park",
			CLIENT_SECRET,
			"zz-access-token",
		):
			self.assertNotIn(secret, logged)

	def get_create_log(self) -> frappe._dict:
		(log,) = frappe.get_all(
			"Integration Request",
			filters={"integration_request_service": "Qikink", "data": ["like", f"%{CREATE_ORDER_PATH}%"]},
			fields=["output", "error", "status"],
		)
		return log

	def test_a_refused_call_logs_its_status_code(self):
		self.qikink.create_replies = [
			make_reply(400, {"error": "Missing field: phone", "status_code": "400"})
		]

		self.push(self.place_prepaid_order())

		log = self.get_create_log()
		self.assertEqual(
			(json.loads(log.output), log.error, log.status), ({"status_code": 400}, "HTTP 400", "Failed")
		)

	def test_a_call_that_times_out_logs_the_timeout(self):
		self.qikink.create_replies = [requests.Timeout("Read timed out")]

		self.push(self.place_prepaid_order())

		log = self.get_create_log()
		self.assertEqual((log.error, log.status), ("Timeout", "Failed"))


class TestQikinkIndicator(QikinkPushTestCase):
	"""Staff open the Qikink Order from the Sales Order and the Purchase Order."""

	def test_the_order_forms_link_to_the_qikink_order(self):
		sales_order = self.place_prepaid_order()
		qikink_order = self.push(sales_order)

		for doctype, name in (
			("Sales Order", sales_order.name),
			("Purchase Order", qikink_order.purchase_order),
		):
			self.assertEqual(
				self.get_form_onload(doctype, name).get("qikink_order"),
				{
					"name": qikink_order.name,
					"order_number": qikink_order.order_number,
					"status": "Pushed",
					"is_pushable": False,
				},
			)

	def test_a_user_who_cannot_open_qikink_orders_sees_no_link(self):
		sales_order = self.place_prepaid_order()
		self.push(sales_order)
		frappe.set_user(create_staff("Sales User"))

		self.assertNotIn("qikink_order", self.get_form_onload("Sales Order", sales_order.name))
