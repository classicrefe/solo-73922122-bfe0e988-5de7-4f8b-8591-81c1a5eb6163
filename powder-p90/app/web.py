"""Flask 网页：按批号上传/补报档表，勾选批次核算合并 P90 最紧界。"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import asdict

from flask import Flask, flash, redirect, render_template, request, url_for

from .core import BinTableError, merge_p90
from .db import (
    add_table_to_batch,
    connect,
    init_db,
    list_batches,
    list_tables,
    load_batch_tables,
    load_table,
    parse_and_preview,
)

log = logging.getLogger(__name__)


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "powder-p90-demo-secret")
    app.config["DB_PATH"] = db_path or os.environ.get(
        "POWDER_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "powder.db")
    )
    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)
    init_db(app.config["DB_PATH"])

    @app.route("/")
    def index():
        with connect(app.config["DB_PATH"]) as conn:
            batches = [dict(r) for r in list_batches(conn)]
        return render_template("index.html", batches=batches)

    @app.route("/upload", methods=["POST"])
    def upload():
        name = (request.form.get("batch_name") or "").strip()
        text = request.form.get("table_text") or ""
        note = (request.form.get("note") or "").strip()
        if not name:
            flash("批号不能为空", "error")
            return redirect(url_for("index"))
        try:
            table = parse_and_preview(text)
        except BinTableError as exc:
            flash(f"档表解析失败，未入库：{exc}", "error")
            return redirect(url_for("index"))

        try:
            outcome = add_table_to_batch(app.config["DB_PATH"], name, table, note)
        except (sqlite3.DatabaseError, OSError) as exc:
            # 锁等待超时、磁盘/权限等基础设施故障：不返回 500，提示稍后重试
            log.exception("upload for batch %r failed", name)
            flash(f"系统繁忙，本次上传未确认入库，请稍后重试：{exc}", "error")
            return redirect(url_for("index"))

        if not outcome.ok:
            # 补报失败：不写入，后续核算仍只按此前已入库档表
            flash(
                f"补报被拒绝（与批 {name} 已入库的 {outcome.existing_count} 张档表不相容）："
                f"{outcome.reason}。本次上传未入库，核算仍按原档表进行。",
                "error",
            )
        elif outcome.created_batch:
            flash(
                f"新批 {name} 已创建，档表 #{outcome.table_id} 入库：{len(table.bins)} 档、"
                f"{table.total} 粒",
                "ok",
            )
        else:
            flash(
                f"批 {name} 补报成功：档表 #{outcome.table_id} 与已有 "
                f"{outcome.existing_count} 张档表相容，"
                f"现共 {outcome.existing_count + 1} 张档表、{table.total} 粒",
                "ok",
            )
        return redirect(url_for("index"))

    @app.route("/batch/<int:batch_id>")
    def batch_detail(batch_id: int):
        with connect(app.config["DB_PATH"]) as conn:
            trows = list_tables(conn, batch_id)
            tables = [
                {
                    "meta": dict(r),
                    "bins": [asdict(b) for b in load_table(conn, int(r["id"])).bins],
                }
                for r in trows
            ]
            batches = [dict(r) for r in list_batches(conn)]
            name = next((b["name"] for b in batches if b["id"] == batch_id), str(batch_id))
        return render_template("batch.html", name=name, batch_id=batch_id, tables=tables)

    def _compute_context(db_path: str, ids: list[int]):
        with connect(db_path) as conn:
            all_batches = {int(r["id"]): dict(r) for r in list_batches(conn)}
            missing = [i for i in ids if i not in all_batches]
            if missing:
                return f"批次不存在：{missing}"
            batch_tables = [(bid, load_batch_tables(conn, bid)) for bid in ids]
            if any(not ts for _, ts in batch_tables):
                return "所选批次中存在没有任何档表的批，无法核算"
            try:
                result = merge_p90(batch_tables)
            except BinTableError as exc:
                return f"核算失败：{exc}"

            # 逐批见证频数整理为有序列表，并附每批名称
            def witness_rows(witness):
                out = []
                for bid in ids:
                    freq = witness[bid]
                    rows = [
                        {"size": s, "count": freq[s]} for s in sorted(freq) if freq[s]
                    ]
                    out.append(
                        {
                            "batch_id": bid,
                            "batch_name": all_batches[bid]["name"],
                            "total": sum(freq.values()),
                            "rows": rows,
                        }
                    )
                return out

            return {
                "names": {bid: all_batches[bid]["name"] for bid in ids},
                "ids": ids,
                "total": result.total,
                "k": result.k,
                "p90_low": result.p90_low,
                "p90_high": result.p90_high,
                "witness_low": witness_rows(result.witness_low),
                "witness_high": witness_rows(result.witness_high),
            }

    @app.route("/compute", methods=["POST"])
    def compute():
        try:
            ids = [int(x) for x in request.form.getlist("batch_ids")]
        except ValueError:
            flash("批次编号无效", "error")
            return redirect(url_for("index"))
        if not ids:
            flash("请至少勾选一个批次", "error")
            return redirect(url_for("index"))
        try:
            ctx = _compute_context(app.config["DB_PATH"], ids)
        except sqlite3.DatabaseError as exc:
            log.exception("compute failed")
            flash(f"系统繁忙，核算暂时无法完成，请稍后重试：{exc}", "error")
            return redirect(url_for("index"))
        if isinstance(ctx, str):  # 校验失败消息
            flash(ctx, "error")
            return redirect(url_for("index"))
        return render_template("result.html", **ctx)

    return app
