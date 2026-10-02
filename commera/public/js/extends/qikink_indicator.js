frappe.provide('commera.qikink');

commera.qikink.STATUS_COLORS = {
	Queued: 'orange',
	Pushed: 'blue',
	Failed: 'red',
	'Needs Attention': 'red',
	Completed: 'green',
	Cancelled: 'gray',
};

commera.qikink.show_indicator = (frm) => {
	const qikink_order = frm.doc.__onload?.qikink_order;
	if (!qikink_order) {
		return;
	}

	const label = __('Qikink {0}: {1}', [
		qikink_order.order_number,
		__(qikink_order.status),
	]);
	frm.dashboard.add_indicator(
		frappe.utils.get_form_link('Qikink Order', qikink_order.name, true, label),
		commera.qikink.STATUS_COLORS[qikink_order.status],
	);
};

commera.qikink.add_push_button = (frm) => {
	const qikink_order = frm.doc.__onload?.qikink_order;
	if (!qikink_order?.is_pushable) {
		return;
	}

	frm.add_custom_button(__('Push to Qikink'), async () => {
		await frappe.xcall('run_doc_method', {
			dt: 'Qikink Order',
			dn: qikink_order.name,
			method: 'requeue_push',
		});
		frm.reload_doc();
	});
};
