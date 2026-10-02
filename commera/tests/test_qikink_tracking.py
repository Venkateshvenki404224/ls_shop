# Copyright (c) 2026, company@bwhstudios.com and Contributors

import frappe

from commera.api.admin.orders import fulfil_order
from commera.api.shipping import get_order_tracking
from commera.order_access import set_order_access_key
from commera.tests.test_order_status import book_parcel
from commera.tests.test_qikink_push import BUYING_RATE
from commera.tests.test_qikink_sync import AWB, SHIPPED, TRACKING_LINK, QikinkSyncTestCase
from commera.utils import update_sales_order_ecommerce_status

WAREHOUSE_AWB = "ZZ-WAREHOUSE-AWB-1"
QIKINK_PARCEL = (AWB, "Bluedart Surface", "In-Transit", TRACKING_LINK)


def get_parcel_details(parcel: dict) -> tuple:
	return (parcel["awb"], parcel["carrier"], parcel["status"], parcel["tracking_link"])


class QikinkTrackingTestCase(QikinkSyncTestCase):
	def place_prepaid_order(self):
		sales_order = super().place_prepaid_order()
		# The live webhook runs as Guest, so it hands the order to the shopper of the cart.
		sales_order.db_set("owner", self.shopper, update_modified=False)
		return sales_order

	def get_ecommerce_status(self, qikink_order) -> str:
		return frappe.db.get_value("Sales Order", qikink_order.sales_order, "custom_ecommerce_status")


class QikinkOnlyTestCase(QikinkTrackingTestCase):
	"""The shopper buys only the Qikink variant, and Qikink ships it."""

	def get_cart_lines(self) -> list[dict]:
		return [self.cart_line(self.variant, 1)]


class TestQikinkOnlyOrderStatus(QikinkOnlyTestCase):
	def test_the_order_status_follows_the_qikink_parcel(self):
		qikink_order = self.place_pushed_order()
		ecommerce_statuses = {}

		for qikink_status in ("Live", "Dispatch Ready", "In-Transit", "Delivered"):
			self.sync_status(qikink_order, qikink_status)
			ecommerce_statuses[qikink_status] = self.get_ecommerce_status(qikink_order)

		self.assertEqual(
			ecommerce_statuses,
			{
				"Live": "Order Received",
				"Dispatch Ready": "Preparing for Shipment",
				"In-Transit": "Shipped",
				"Delivered": "Delivered",
			},
		)

	def test_a_problem_leaves_the_order_status_as_it_is(self):
		qikink_order = self.sync_status(self.place_pushed_order(), "In-Transit")

		for problem_status in ("Lost", "RTO Initiated"):
			qikink_order = self.sync_status(qikink_order, problem_status)

		self.assertEqual(qikink_order.status, "Needs Attention")
		self.assertEqual(self.get_ecommerce_status(qikink_order), "Shipped")

	def test_a_parcel_that_comes_back_returns_the_order(self):
		for qikink_status in ("Returned", "Partially Returned"):
			with self.subTest(qikink_status=qikink_status):
				qikink_order = self.sync_status(self.place_pushed_order(), qikink_status)
				self.assertEqual(self.get_ecommerce_status(qikink_order), qikink_status)


class TestMixedOrderStatus(QikinkTrackingTestCase):
	"""The warehouse ships the warehouse item, and Qikink ships the Qikink variant."""

	def get_status_after_sync(self, qikink_status: str, carrier_status: str | None) -> str:
		qikink_order = self.place_pushed_order()
		if carrier_status:
			book_parcel(qikink_order.sales_order, carrier_status)
		self.sync_status(qikink_order, qikink_status)
		return self.get_ecommerce_status(qikink_order)

	def test_the_order_is_as_far_as_its_slower_parcel(self):
		for qikink_status, carrier_status, ecommerce_status in (
			("Delivered", None, "Order Received"),
			("Dispatch Ready", "In Transit", "Preparing for Shipment"),
			("Delivered", "In Transit", "Shipped"),
			("In-Transit", "Delivered", "Shipped"),
			("Delivered", "Delivered", "Delivered"),
		):
			with self.subTest(qikink_status=qikink_status, carrier_status=carrier_status):
				self.assertEqual(self.get_status_after_sync(qikink_status, carrier_status), ecommerce_status)

	def test_the_warehouse_parcel_that_arrives_last_delivers_the_order(self):
		qikink_order = self.sync_status(self.place_pushed_order(), "Delivered")
		self.assertEqual(self.get_ecommerce_status(qikink_order), "Order Received")
		# The test Bin has no stock ledger behind it, so nothing gives the stock its quantity or its value.
		frappe.db.set_value(
			"Item", self.warehouse_item, {"allow_negative_stock": 1, "valuation_rate": BUYING_RATE}
		)

		fulfil_order(qikink_order.sales_order)
		# The Delivery Note queues this for after the commit.
		update_sales_order_ecommerce_status(qikink_order.sales_order)

		self.assertEqual(self.get_ecommerce_status(qikink_order), "Delivered")

	def test_a_parcel_that_comes_back_makes_a_partial_return(self):
		for qikink_status, carrier_status, ecommerce_status in (
			("Returned", "Delivered", "Partially Returned"),
			("Returned", None, "Partially Returned"),
			("Delivered", "RTO", "Partially Returned"),
			("Partially Returned", "Delivered", "Partially Returned"),
			("Returned", "RTO", "Returned"),
		):
			with self.subTest(qikink_status=qikink_status, carrier_status=carrier_status):
				self.assertEqual(self.get_status_after_sync(qikink_status, carrier_status), ecommerce_status)


class TestQikinkTracking(QikinkOnlyTestCase):
	def place_shipped_order(self):
		qikink_order = self.sync_status(self.place_pushed_order(), "Printed")
		return self.sync_status(qikink_order, "In-Transit", **SHIPPED)

	def test_the_owner_tracks_the_qikink_parcel(self):
		qikink_order = self.place_shipped_order()
		frappe.set_user(self.shopper)

		tracking = get_order_tracking(qikink_order.sales_order)

		self.assertTrue(tracking["has_tracking"])
		self.assertEqual(get_parcel_details(tracking), QIKINK_PARCEL)
		self.assertEqual([event.status for event in tracking["events"]], ["In-Transit", "Printed"])
		(shipment,) = tracking["shipments"]
		self.assertEqual((get_parcel_details(shipment), shipment.events), (QIKINK_PARCEL, tracking["events"]))

	def test_a_tracking_link_that_is_no_web_address_is_not_shown(self):
		qikink_order = self.place_pushed_order()

		for tracking_link in ("javascript:alert(document.cookie)", "NA"):
			with self.subTest(tracking_link=tracking_link):
				self.sync_status(qikink_order, "In-Transit", **{**SHIPPED, "tracking_link": tracking_link})
				tracking = get_order_tracking(qikink_order.sales_order)
				self.assertEqual((tracking["awb"], tracking["tracking_link"]), (AWB, None))

	def test_a_guest_with_the_private_order_link_tracks_the_parcel(self):
		qikink_order = self.place_shipped_order()
		key = set_order_access_key(frappe.get_doc("Sales Order", qikink_order.sales_order))
		frappe.set_user("Guest")

		tracking = get_order_tracking(qikink_order.sales_order, key)

		self.assertEqual(get_parcel_details(tracking), QIKINK_PARCEL)

	def test_a_guest_without_the_private_order_key_is_refused(self):
		qikink_order = self.place_shipped_order()
		set_order_access_key(frappe.get_doc("Sales Order", qikink_order.sales_order))
		frappe.set_user("Guest")

		for key in (None, "zz-not-the-order-key"):
			with self.subTest(key=key), self.assertRaises(frappe.PermissionError):
				get_order_tracking(qikink_order.sales_order, key)


class TestMixedOrderTracking(QikinkTrackingTestCase):
	"""The warehouse parcel goes with a carrier, and Qikink ships the Qikink parcel."""

	def get_shipment_details(self, tracking: dict) -> list[tuple]:
		return [get_parcel_details(shipment) for shipment in tracking["shipments"]]

	def test_each_parcel_shows_its_tracking(self):
		qikink_order = self.sync_status(self.place_pushed_order(), "In-Transit", **SHIPPED)
		book_parcel(qikink_order.sales_order, "In Transit", awb=WAREHOUSE_AWB, carrier="ZZ Carrier")
		frappe.set_user(self.shopper)

		tracking = get_order_tracking(qikink_order.sales_order)

		self.assertEqual(
			self.get_shipment_details(tracking),
			[(WAREHOUSE_AWB, "ZZ Carrier", "In Transit", None), QIKINK_PARCEL],
		)
		self.assertEqual((tracking["has_tracking"], tracking["awb"]), (True, WAREHOUSE_AWB))

	def test_the_tracking_shows_the_first_parcel_that_has_an_awb(self):
		qikink_order = self.sync_status(self.place_pushed_order(), "In-Transit", **SHIPPED)
		book_parcel(qikink_order.sales_order, "Ready To Ship")

		tracking = get_order_tracking(qikink_order.sales_order)

		self.assertEqual(get_parcel_details(tracking), QIKINK_PARCEL)
		self.assertEqual([event.status for event in tracking["events"]], ["In-Transit"])

	def test_an_order_without_an_awb_has_no_tracking(self):
		qikink_order = self.sync_status(self.place_pushed_order(), "Printed")
		book_parcel(qikink_order.sales_order, "Ready To Ship")

		tracking = get_order_tracking(qikink_order.sales_order)

		self.assertFalse(tracking["has_tracking"])
		self.assertEqual(
			self.get_shipment_details(tracking),
			[(None, None, "Ready To Ship", None), (None, None, "Printed", None)],
		)
