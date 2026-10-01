# Copyright (c) 2026, company@bwhstudios.com and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class QikinkSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from commera.commera_ecommerce.doctype.qikink_notify_user.qikink_notify_user import QikinkNotifyUser

		box_packing: DF.Check
		brand_logo: DF.AttachImage | None
		client_id: DF.Data | None
		client_secret: DF.Password | None
		enabled: DF.Check
		notify_users: DF.TableMultiSelect[QikinkNotifyUser]
		sandbox: DF.Check
		supplier: DF.Link | None
	# end: auto-generated types

	@property
	def mode(self) -> str:
		return "sandbox" if self.sandbox else "live"

	def validate(self):
		# Qikink fetches the logo itself, so it must be able to read it.
		if self.brand_logo and self.brand_logo.startswith("/private/"):
			frappe.throw(_("Upload the brand logo as a public file. Qikink cannot fetch a private file."))
