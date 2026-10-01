# Copyright (c) 2026, company@bwhstudios.com and Contributors

import frappe
from erpnext.controllers.item_variant import create_variant
from frappe.desk.form.load import getdoc
from frappe.tests import IntegrationTestCase
from frappe.utils.password import get_decrypted_password

from commera.install import TEST_COMPANY
from commera.tests import create_staff
from commera.tests.qikink import CLIENT_SECRET, SETTINGS, configure_qikink
from commera.tests.test_drop_ship import DropShipTestCase

ATTRIBUTE = "ZZ Qikink Size"


class TestQikinkSettings(IntegrationTestCase):
	"""A store owner connects the Qikink account in one System Manager form."""

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		configure_qikink(self, supplier=None, enabled=0)

	def test_the_client_secret_never_reaches_the_browser(self):
		frappe.local.response = frappe._dict(docs=[])

		getdoc(SETTINGS, SETTINGS)

		self.assertNotIn(CLIENT_SECRET, frappe.as_json(frappe.response.docs))
		self.assertEqual(get_decrypted_password(SETTINGS, SETTINGS, "client_secret"), CLIENT_SECRET)

	def test_the_brand_logo_must_be_a_public_file(self):
		with self.assertRaisesRegex(frappe.ValidationError, "public"):
			configure_qikink(self, supplier=None, enabled=0, brand_logo="/private/files/zz-brand-logo.png")

	def test_only_a_system_manager_opens_qikink_settings(self):
		self.assertTrue(frappe.has_permission(SETTINGS, "read", user=create_staff("System Manager")))
		self.assertFalse(frappe.has_permission(SETTINGS, "read", user=create_staff("Sales Manager")))


class TestQikinkItem(DropShipTestCase):
	"""A catalog manager marks Qikink products with the ERPNext drop-ship tick and default supplier."""

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.supplier = self.create_supplier()
		configure_qikink(self, supplier=self.supplier)
		if not frappe.db.exists("Item Attribute", ATTRIBUTE):
			frappe.get_doc(
				{
					"doctype": "Item Attribute",
					"attribute_name": ATTRIBUTE,
					"item_attribute_values": [{"attribute_value": "S", "abbr": "S"}],
				}
			).insert(ignore_permissions=True)

	def qikink_values(self, supplier: str | None = None) -> dict:
		return {
			"delivered_by_supplier": 1,
			"item_defaults": [{"company": TEST_COMPANY, "default_supplier": supplier or self.supplier}],
		}

	def create_template(self) -> str:
		return self.create_item(
			"QIKINK-TEMPLATE", has_variants=1, attributes=[{"attribute": ATTRIBUTE}], **self.qikink_values()
		)

	def new_variant(self):
		"""A variant that takes the tick and the supplier from its template only."""
		variant = create_variant(self.create_template(), {ATTRIBUTE: "S"})
		variant.delivered_by_supplier = 0
		variant.item_defaults = []
		return variant

	def test_the_qikink_sku_shows_only_on_a_drop_ship_item(self):
		field = frappe.get_meta("Item").get_field("custom_qikink_sku")

		self.assertEqual(
			(field.depends_on, field.length),
			("eval:doc.delivered_by_supplier || (doc.__onload && doc.__onload.template_is_drop_ship)", 50),
		)

	def test_the_item_form_knows_a_variant_takes_the_tick_from_its_template(self):
		variant = self.new_variant()
		variant.custom_qikink_sku = "ZZ-SKU-S"
		variant.insert(ignore_permissions=True)
		frappe.local.response = frappe._dict(docs=[])

		getdoc("Item", variant.name)

		self.assertTrue(frappe.response.docs[0].get_onload().get("template_is_drop_ship"))

	def test_a_qikink_variant_needs_a_qikink_sku(self):
		variant = self.new_variant()

		with self.assertRaisesRegex(frappe.ValidationError, "Qikink SKU"):
			variant.insert(ignore_permissions=True)

		variant.custom_qikink_sku = "ZZ-SKU-S"
		variant.insert(ignore_permissions=True)

	def test_a_qikink_product_without_variants_needs_a_qikink_sku(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Qikink SKU"):
			self.create_item("QIKINK", **self.qikink_values())

		self.create_item("QIKINK", custom_qikink_sku="ZZ-SKU", **self.qikink_values())

	def test_a_qikink_template_needs_no_qikink_sku(self):
		self.assertTrue(frappe.db.exists("Item", self.create_template()))

	def test_a_qikink_item_cannot_hold_stock(self):
		with self.assertRaisesRegex(frappe.ValidationError, "stock item"):
			self.create_item("QIKINK", is_stock_item=1, custom_qikink_sku="ZZ-SKU", **self.qikink_values())

	def test_a_drop_ship_item_of_another_supplier_needs_no_qikink_sku(self):
		other_supplier = self.create_supplier()

		item_code = self.create_item("DROPSHIP", is_stock_item=1, **self.qikink_values(other_supplier))

		self.assertTrue(frappe.db.exists("Item", item_code))
