# Copyright (c) 2026, company@bwhstudios.com and Contributors

import frappe
from frappe.tests import IntegrationTestCase

from commera.api.payments import (
	add_billing_address,
	add_shipping_address,
	generate_quotation_for_cart,
	update_quotation_address,
)
from commera.core import _get_cart_quotation
from commera.install import TEST_ITEM_GROUP
from commera.shop_themes.render import render_themed_template
from commera.tests import create_shopper
from commera.www.cart.checkout import get_context as get_checkout_context

DEFAULT_THEME = "Shop Default Theme"
ITEM_RATE = 120.0


class TestCheckoutState(IntegrationTestCase):
	"""The state on the checkout address, as the default-theme checkout asks for it and saves it."""

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")
		self.shopper = create_shopper()
		item_code = self.create_stocked_item()
		frappe.set_user(self.shopper)
		cart_line = {"item": {"display_name": "ZZ Cart Item"}, "variant": {"item_code": item_code}, "qty": 1}
		generate_quotation_for_cart({"items": [cart_line]})

	# -- fixtures ---------------------------------------------------------------------------------

	def create_stocked_item(self) -> str:
		commera_settings = frappe.get_cached_doc("Commera Settings")
		item_code = f"ZZ-STATE-{frappe.generate_hash(length=8)}"
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "ZZ State Item",
				"item_group": TEST_ITEM_GROUP,
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": commera_settings.get_default_price_list(),
				"price_list_rate": ITEM_RATE,
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Bin",
				"item_code": item_code,
				"warehouse": commera_settings.ecommerce_warehouse,
				"actual_qty": 4,
			}
		).insert(ignore_permissions=True)
		return item_code

	def new_address(self, street: str, state: str) -> dict:
		return {
			"is_saved": False,
			"first_name": "ZZ",
			"last_name": "Shopper",
			"email": self.shopper,
			"phone_number": "+919800000001",
			"full_address": street,
			"landmark": "Near the park",
			"country": "India",
			"city": "Chennai",
			"state": state,
			"po_box": "600001",
		}

	def saved_states(self) -> tuple[str, str]:
		quotation = _get_cart_quotation()
		return (
			frappe.db.get_value("Address", quotation.customer_address, "state"),
			frappe.db.get_value("Address", quotation.shipping_address_name, "state"),
		)

	def render_default_checkout(self) -> str:
		context = frappe._dict()
		get_checkout_context(context)
		return render_themed_template("theme://pages/cart/checkout.html", context, theme_name=DEFAULT_THEME)

	# -- tests ------------------------------------------------------------------------------------

	def test_the_default_checkout_asks_for_the_billing_and_shipping_state(self):
		html = self.render_default_checkout()

		for prefix in ("billing", "shipping"):
			self.assertIn(f'name="{prefix}_state"', html)
		# One India list for billing and one for shipping, states and union territories alike.
		self.assertEqual(html.count('<option value="Tamil Nadu">'), 2)
		self.assertEqual(html.count('<option value="Andaman and Nicobar Islands">'), 2)

	def test_new_billing_and_shipping_addresses_keep_their_states(self):
		update_quotation_address(
			{
				"billing_address": self.new_address("1 Billing Street", "Tamil Nadu"),
				"shipping_address": self.new_address("2 Shipping Street", "Karnataka"),
				"shipping_same_as_billing": False,
			}
		)

		self.assertEqual(self.saved_states(), ("Tamil Nadu", "Karnataka"))

	def test_shipping_same_as_billing_ships_to_the_billing_state(self):
		update_quotation_address(
			{
				"billing_address": self.new_address("1 Billing Street", "Kerala"),
				"shipping_address": {"is_saved": True, "address_id": None},
				"shipping_same_as_billing": True,
			}
		)

		self.assertEqual(self.saved_states(), ("Kerala", "Kerala"))

	def test_an_address_saved_before_states_were_asked_is_still_accepted(self):
		customer = _get_cart_quotation().party_name
		old_address = self.new_address("1 Old Street", state="")
		billing_address = add_billing_address(customer, {"billing_address": old_address}).name
		shipping_address = add_shipping_address(customer, {"shipping_address": old_address}).name

		update_quotation_address(
			{
				"billing_address": {"is_saved": True, "address_id": billing_address},
				"shipping_address": {"is_saved": True, "address_id": shipping_address},
				"shipping_same_as_billing": False,
			}
		)

		quotation = _get_cart_quotation()
		self.assertEqual(
			(quotation.customer_address, quotation.shipping_address_name), (billing_address, shipping_address)
		)
