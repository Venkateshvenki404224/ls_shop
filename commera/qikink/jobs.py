import frappe

from commera.commera_ecommerce.doctype.qikink_order.qikink_order import MAX_ATTEMPTS

SYNC_JOB = "commera.qikink.jobs.sync_qikink_orders"
PUSH_SAVEPOINT = "qikink_sync_push"


def push_qikink_order(qikink_order: str) -> None:
	frappe.get_doc("Qikink Order", qikink_order).push()


def queue_qikink_sync() -> None:
	"""A cron event runs on the default queue, so it hands the sync to the long queue."""
	frappe.enqueue(SYNC_JOB, queue="long", job_id="qikink_sync", deduplicate=True)


def sync_qikink_orders() -> None:
	"""Retry the queued pushes. Each order commits alone, so one broken order cannot undo another."""
	if not frappe.get_cached_doc("Qikink Settings").enabled:
		return
	waiting_orders = frappe.get_all(
		"Qikink Order",
		filters={"status": "Queued", "attempts": ["<", MAX_ATTEMPTS]},
		order_by="creation asc",
		pluck="name",
	)
	for qikink_order in waiting_orders:
		try:
			frappe.db.savepoint(PUSH_SAVEPOINT)
			push_qikink_order(qikink_order)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback(save_point=PUSH_SAVEPOINT)
			frappe.log_error(
				title="Qikink push failed", reference_doctype="Qikink Order", reference_name=qikink_order
			)
			frappe.db.commit()
