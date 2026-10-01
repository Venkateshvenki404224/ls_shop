# Copyright (c) 2026, company@bwhstudios.com and Contributors

from unittest.mock import patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_purchase_order
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate

from commera.api.admin.orders import fulfil_order, get_order
from commera.api.cart import get_detail_for_cart_items, get_stock_shortfalls
from commera.api.payment_hooks import on_payment_request_update
from commera.api.payments import (
	COD_PAYMENT_MODE,
	confirm_payment,
	generate_quotation_for_cart,
	initiate_checkout_with_mode,
	update_quotation_address,
)
from commera.api.shipping import get_charge_amount
from commera.core import _get_cart_quotation
from commera.install import TEST_ABBR, TEST_COMPANY, TEST_ITEM_GROUP
from commera.product_detail import get_product_detail
from commera.tests import create_shopper, get_test_configurator
from commera.tests.test_admin_orders import ensure_fiscal_year
from commera.utils import get_available_stocks

DROP_SHIP_STOCK_QTY = 100
IN_STOCK_QTY = 4.0
ITEM_RATE = 250.0
GATEWAY = "ZZ Drop Ship Card"
LEAD_TIME_DAYS = 5


class DropShipTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.warehouse = frappe.get_cached_doc("Commera Settings").ecommerce_warehouse

	def create_item(self, label: str, **values) -> str:
		item_code = f"ZZ-{label}-{frappe.generate_hash(length=8)}"
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": f"ZZ {label}",
				"item_group": TEST_ITEM_GROUP,
				"stock_uom": "Nos",
				"is_stock_item": 0,
				**values,
			}
		).insert(ignore_permissions=True)
		return item_code

	def create_supplier(self) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": f"ZZ Drop Ship Supplier {frappe.generate_hash(length=8)}",
					"supplier_group": frappe.get_all("Supplier Group", {"is_group": 0}, pluck="name")[0],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def create_drop_ship_variant(self, supplier: str | None = None) -> str:
		"""A variant that carries no tick or supplier of its own: the catalog manager set both on the template."""
		item_defaults = [{"company": TEST_COMPANY, "default_supplier": supplier}] if supplier else []
		template = self.create_item(
			"DROPSHIP-TEMPLATE",
			delivered_by_supplier=1,
			item_defaults=item_defaults,
			lead_time_days=LEAD_TIME_DAYS,
		)
		variant = self.create_item("DROPSHIP-VARIANT")
		frappe.db.set_value("Item", template, "has_variants", 1)
		frappe.db.set_value("Item", variant, "variant_of", template)
		return variant

	def create_warehouse_item(self, label: str = "WAREHOUSE") -> str:
		item_code = self.create_item(label, is_stock_item=1)
		# Bin is what get_available_stocks reads; ERPNext creates it the same way.
		frappe.get_doc(
			{
				"doctype": "Bin",
				"item_code": item_code,
				"warehouse": self.warehouse,
				"actual_qty": IN_STOCK_QTY,
			}
		).insert(ignore_permissions=True)
		return item_code

	def cart_line(self, item_code: str, qty: float) -> dict:
		return {"item": {"display_name": "ZZ Cart Item"}, "variant": {"item_code": item_code}, "qty": qty}


class TestDropShipStock(DropShipTestCase):
	def test_a_drop_ship_variant_is_in_stock_without_a_bin(self):
		variant = self.create_drop_ship_variant()

		stock = get_available_stocks([variant], self.warehouse)

		self.assertEqual(stock[variant], {"stock_qty": DROP_SHIP_STOCK_QTY, "in_stock": 1})

	def test_an_item_ticked_on_itself_is_in_stock_without_a_bin(self):
		item_code = self.create_item("DROPSHIP", delivered_by_supplier=1)

		stock = get_available_stocks([item_code], self.warehouse)

		self.assertEqual(stock[item_code], {"stock_qty": DROP_SHIP_STOCK_QTY, "in_stock": 1})

	def test_a_warehouse_item_keeps_its_bin_stock(self):
		warehouse_item = self.create_warehouse_item()
		variant = self.create_drop_ship_variant()

		stock = get_available_stocks([warehouse_item, variant], self.warehouse)

		self.assertEqual(stock[warehouse_item], {"stock_qty": IN_STOCK_QTY, "in_stock": 1})

	def test_many_drop_ship_items_cost_the_same_queries_as_one(self):
		variants = [self.create_drop_ship_variant() for _ in range(3)]
		get_available_stocks(variants[:1], self.warehouse)

		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as one_item:
			get_available_stocks(variants[:1], self.warehouse)
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as three_items:
			get_available_stocks(variants, self.warehouse)

		self.assertEqual(three_items.call_count, one_item.call_count)

	def test_the_cart_sells_up_to_the_drop_ship_quantity(self):
		variant = self.create_drop_ship_variant()

		detail = get_detail_for_cart_items([self.cart_line(variant, 1)])

		self.assertEqual(detail["stock_data"][variant]["stock"], DROP_SHIP_STOCK_QTY)
		self.assertEqual(get_stock_shortfalls([self.cart_line(variant, DROP_SHIP_STOCK_QTY)]), [])
		self.assertEqual(len(get_stock_shortfalls([self.cart_line(variant, DROP_SHIP_STOCK_QTY + 1)])), 1)

	def test_the_product_page_shows_a_drop_ship_product_in_stock(self):
		variant = self.create_drop_ship_variant()
		route = f"zz-drop-ship-{frappe.generate_hash(length=8)}"
		product = frappe.new_doc("Style Attribute Variant")
		product.update(
			{
				"configurator": get_test_configurator(),
				"item_style": frappe.db.get_value("Item", variant, "variant_of"),
				"item_group": TEST_ITEM_GROUP,
				"attribute_value": f"ZZ {route}",
				"display_name": "ZZ Drop Ship Product",
				"route": route,
				"is_published": 1,
			}
		)
		# Images and sizes are required, or validate() unpublishes the product.
		product.append("images", {"image": "/assets/drop-ship-test.jpg"})
		product.append("sizes", {"size": "M", "item_code": variant})
		product.insert(ignore_permissions=True)

		detail = get_product_detail(route, selected_size="M")

		self.assertTrue(detail["in_stock"])
		self.assertEqual(detail["selected_item"]["stock_detail"]["stock_qty"], DROP_SHIP_STOCK_QTY)


class DropShipCheckoutTestCase(DropShipTestCase):
	"""A shopper buys a drop-ship variant and a warehouse item in one cart."""

	def setUp(self):
		ensure_fiscal_year()
		self.addCleanup(frappe.set_user, "Administrator")
		self.supplier = self.create_supplier()
		self.variant = self.create_drop_ship_variant(self.supplier)
		self.warehouse_item = self.create_warehouse_item()
		for item_code in (self.variant, self.warehouse_item):
			self.create_sale_price(item_code)
		self.shopper = create_shopper()

	def create_sale_price(self, item_code: str):
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": frappe.get_cached_doc("Commera Settings").get_sale_price_list(),
				"price_list_rate": ITEM_RATE,
			}
		).insert(ignore_permissions=True)

	def fill_cart(self):
		"""The shopper's own cart, addressed and ready to pay, holding one line of each kind."""
		frappe.set_user(self.shopper)
		generate_quotation_for_cart(
			{"items": [self.cart_line(self.variant, 1), self.cart_line(self.warehouse_item, 2)]}
		)
		update_quotation_address(
			{"billing_address": self.get_billing_address(), "shipping_same_as_billing": True}
		)
		return _get_cart_quotation()

	def get_billing_address(self) -> dict:
		return {
			"full_address": "1 Billing Street",
			"city": "Chennai",
			"country": "India",
			"phone_number": "+919800000001",
			"email": self.shopper,
			"first_name": "ZZ",
			"last_name": "Shopper",
		}

	def place_cod_order(self, cod_charge: float = 0):
		quotation = self.fill_cart()
		charge_account_head = frappe.db.get_value(
			"Account", {"company": TEST_COMPANY, "root_type": "Income", "is_group": 0}, "name"
		)
		frappe.db.set_single_value(
			"Commera Settings",
			{
				"cod_enabled": 1,
				"cod_charge": cod_charge,
				"cod_charge_applicable_below": 100000,
				"charge_account_head": charge_account_head,
			},
		)
		frappe.clear_document_cache("Commera Settings", "Commera Settings")
		self.addCleanup(frappe.clear_document_cache, "Commera Settings", "Commera Settings")

		initiate_checkout_with_mode(COD_PAYMENT_MODE)
		order_name = confirm_payment(quotation.name, payment_mode=COD_PAYMENT_MODE)["order_name"]
		frappe.set_user("Administrator")
		return frappe.get_doc("Sales Order", order_name)

	def place_prepaid_order(self):
		"""The gateway reports the cart paid, and its webhook places the order."""
		quotation = self.fill_cart()
		frappe.set_user("Administrator")
		# The test case rolls back per class, so a second test finds the first one's row.
		if not frappe.db.exists("Mode of Payment", GATEWAY):
			frappe.get_doc(
				{
					"doctype": "Mode of Payment",
					"mode_of_payment": GATEWAY,
					"type": "Bank",
					"accounts": [{"company": TEST_COMPANY, "default_account": f"Cash - {TEST_ABBR}"}],
				}
			).insert(ignore_permissions=True)
		payment_request = frappe.new_doc("Gateway Payment Request")
		payment_request.update(
			{
				"name": frappe.generate_hash(length=10),
				"gateway": GATEWAY,
				"amount": get_charge_amount(quotation),
				"currency_code": quotation.currency,
				"ref_doctype": "Quotation",
				"ref_docname": quotation.name,
				"status": "Paid",
			}
		)
		# db_insert, not insert(): insert() opens a real gateway session.
		payment_request.db_insert()

		on_payment_request_update(payment_request)
		return frappe.get_doc("Sales Order", payment_request.ref_docname)

	def get_line(self, sales_order, item_code: str):
		return next(row for row in sales_order.items if row.item_code == item_code)


class TestDropShipCheckout(DropShipCheckoutTestCase):
	def assert_supplier_delivers(self, line):
		self.assertEqual((line.delivered_by_supplier, line.supplier), (1, self.supplier))

	def test_a_cod_order_has_the_supplier_deliver_the_drop_ship_line(self):
		sales_order = self.place_cod_order()

		self.assert_supplier_delivers(self.get_line(sales_order, self.variant))
		warehouse_line = self.get_line(sales_order, self.warehouse_item)
		self.assertEqual(
			(warehouse_line.delivered_by_supplier, warehouse_line.warehouse), (0, self.warehouse)
		)

	def test_a_prepaid_order_has_the_supplier_deliver_the_drop_ship_line(self):
		sales_order = self.place_prepaid_order()

		self.assertEqual(sales_order.docstatus, 1)
		self.assert_supplier_delivers(self.get_line(sales_order, self.variant))

	def test_a_prepaid_order_reserves_stock_only_for_the_warehouse_line(self):
		sales_order = self.place_prepaid_order()

		warehouse_line = self.get_line(sales_order, self.warehouse_item)
		self.assertEqual(
			(warehouse_line.delivered_by_supplier, warehouse_line.warehouse), (0, self.warehouse)
		)
		self.assertEqual(self.get_reserved_qty(self.warehouse_item), 2)
		self.assertEqual(self.get_reserved_qty(self.variant), 0)

	def get_reserved_qty(self, item_code: str) -> float:
		return sum(frappe.get_all("Bin", filters={"item_code": item_code}, pluck="reserved_qty"))

	def test_an_order_made_in_desk_keeps_the_standard_erpnext_lines(self):
		"""ERPNext copies no drop-ship tick from the Item, so only the checkout step sets it."""
		# ERPNext moved its transaction mappers to a sibling `mapper` module; both layouts are in the wild.
		try:
			from erpnext.selling.doctype.quotation.mapper import make_sales_order
		except ImportError:
			from erpnext.selling.doctype.quotation.quotation import make_sales_order

		# Ticked on the variant itself, so a copy from the Item would show on the line.
		frappe.db.set_value("Item", self.variant, "delivered_by_supplier", 1)
		quotation = self.fill_cart()
		frappe.set_user("Administrator")
		quotation.submit()

		sales_order = make_sales_order(quotation.name)
		sales_order.insert()

		self.assertEqual(self.get_line(sales_order, self.variant).delivered_by_supplier, 0)

	def test_make_purchase_order_ships_the_drop_ship_line_to_the_customer(self):
		sales_order = self.place_prepaid_order()

		purchase_orders = make_purchase_order(
			sales_order.name, selected_items=[{"item_code": self.variant, "supplier": self.supplier}]
		)

		self.assertEqual(len(purchase_orders), 1)
		purchase_order = purchase_orders[0]
		self.assertEqual(purchase_order.supplier, self.supplier)
		self.assertEqual(
			[(row.item_code, row.delivered_by_supplier, row.sales_order) for row in purchase_order.items],
			[(self.variant, 1, sales_order.name)],
		)
		# A Shopping Cart order needs no delivery date; the supplier's lead time gives the PO its Required By.
		self.assertEqual(
			purchase_order.items[0].schedule_date, add_days(sales_order.transaction_date, LEAD_TIME_DAYS)
		)
		self.assertTrue(sales_order.shipping_address_name)
		self.assertEqual(purchase_order.shipping_address, sales_order.shipping_address_name)
		self.assertTrue(sales_order.contact_person)
		self.assertEqual(purchase_order.customer_contact_person, sales_order.contact_person)


class TestDropShipFulfil(DropShipTestCase):
	"""The dashboard "Fulfil" action ships from the warehouse only; the supplier ships the drop-ship lines."""

	def setUp(self):
		ensure_fiscal_year()
		self.supplier = self.create_supplier()
		self.drop_ship_item = self.create_item("DROPSHIP", delivered_by_supplier=1)
		# Non-stock keeps the Delivery Note off the stock ledger; the line split is what is under test.
		self.warehouse_item = self.create_item("WAREHOUSE")

	def create_order(self, *item_codes: str):
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"ZZ Drop Ship Customer {frappe.generate_hash(length=8)}",
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
				"items": [self.order_line(item_code) for item_code in item_codes],
			}
		)
		sales_order.insert(ignore_permissions=True)
		sales_order.submit()
		return sales_order

	def order_line(self, item_code: str) -> dict:
		line = {"item_code": item_code, "qty": 1, "rate": ITEM_RATE}
		if item_code == self.drop_ship_item:
			line.update({"delivered_by_supplier": 1, "supplier": self.supplier})
		return line

	def test_fulfil_ships_only_the_warehouse_lines_of_a_mixed_order(self):
		sales_order = self.create_order(self.drop_ship_item, self.warehouse_item)

		delivery_note = frappe.get_doc("Delivery Note", fulfil_order(sales_order.name)["delivery_note"])

		self.assertEqual([row.item_code for row in delivery_note.items], [self.warehouse_item])

	def test_a_mixed_order_offers_no_fulfil_once_its_warehouse_lines_ship(self):
		sales_order = self.create_order(self.drop_ship_item, self.warehouse_item)
		self.assertTrue(get_order(sales_order.name)["can_fulfil"])

		fulfil_order(sales_order.name)

		self.assertFalse(get_order(sales_order.name)["can_fulfil"])

	def test_fulfil_makes_nothing_for_an_order_the_supplier_delivers(self):
		sales_order = self.create_order(self.drop_ship_item)

		self.assertFalse(get_order(sales_order.name)["can_fulfil"])
		with self.assertRaises(frappe.ValidationError) as raised:
			fulfil_order(sales_order.name)

		self.assertIn("supplier delivers", str(raised.exception))
		self.assertFalse(frappe.db.exists("Delivery Note Item", {"against_sales_order": sales_order.name}))
