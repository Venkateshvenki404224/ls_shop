// Copyright (c) 2026, company@bwhstudios.com and contributors
// For license information, please see license.txt

frappe.ui.form.on('Qikink Order', {
	refresh(frm) {
		if (frm.doc.__onload?.is_pushable) {
			frm.add_custom_button(__('Push to Qikink'), async () => {
				await frm.call('requeue_push');
				frm.reload_doc();
			});
		}
		if (frm.doc.__onload?.is_syncable) {
			frm.add_custom_button(__('Sync now'), async () => {
				await frm.call('sync');
				frm.reload_doc();
			});
		}
	},
});
