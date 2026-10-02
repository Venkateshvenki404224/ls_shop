from collections.abc import Callable

import frappe
from frappe.utils import create_batch

from commera.commera_ecommerce.doctype.qikink_order.qikink_order import (
	MAX_ATTEMPTS,
	SYNCABLE_STATES,
	QikinkOrder,
)
from commera.qikink.client import ORDER_LIST_PAGE_SIZE, QikinkClient

SYNC_JOB = "commera.qikink.jobs.sync_qikink_orders"
ORDER_SAVEPOINT = "qikink_sync_order"


def push_qikink_order(qikink_order: str) -> None:
	frappe.get_doc("Qikink Order", qikink_order).push()


def queue_qikink_sync() -> None:
	"""A cron event runs on the default queue, so it hands the sync to the long queue."""
	frappe.enqueue(SYNC_JOB, queue="long", job_id="qikink_sync", deduplicate=True)


def sync_qikink_orders() -> None:
	"""Retry the queued pushes, then read the open orders at Qikink."""
	if not frappe.get_cached_doc("Qikink Settings").enabled:
		return
	waiting_orders = frappe.get_all(
		"Qikink Order",
		filters={"status": "Queued", "attempts": ["<", MAX_ATTEMPTS]},
		order_by="creation asc",
		pluck="name",
	)
	commit_each(waiting_orders, "Qikink push failed", QikinkOrder.push)
	client = QikinkClient()
	if client.has_order_list:
		sync_open_orders(client)


def sync_open_orders(client: QikinkClient) -> None:
	open_orders = frappe.get_all(
		"Qikink Order",
		filters={"status": ["in", SYNCABLE_STATES]},
		fields=["name", "qikink_order_id"],
		order_by="creation asc",
	)
	for page in create_batch(open_orders, ORDER_LIST_PAGE_SIZE):
		try:
			remote_orders = client.get_orders([order.qikink_order_id for order in page])
		except Exception:
			# A page that fails must not stop the next pages.
			frappe.log_error(title="Qikink order list failed")
			frappe.db.commit()
			continue
		commit_each(
			[order.name for order in page],
			"Qikink sync failed",
			lambda qikink_order: qikink_order.apply_order_list(remote_orders),
		)


def commit_each(names: list[str], error_title: str, change: Callable[[QikinkOrder], None]) -> None:
	"""Commit the change of each Qikink Order alone, so one broken order cannot undo another."""
	for name in names:
		try:
			frappe.db.savepoint(ORDER_SAVEPOINT)
			change(frappe.get_doc("Qikink Order", name))
			frappe.db.commit()
		except Exception:
			frappe.db.rollback(save_point=ORDER_SAVEPOINT)
			frappe.log_error(title=error_title, reference_doctype="Qikink Order", reference_name=name)
			frappe.db.commit()
