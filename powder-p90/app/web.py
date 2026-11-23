"""Flask 网页：按批号上传/补报档表，勾选批次核算合并 P90 最紧界。"""
from __future__ import annotations

import os
from dataclasses import asdict

from flask import Flask, flash, redirect, render_template, request, url_for

from .core import BinTableError, compatible, merge_p90
from .db import (
    connect,
    create_batch,
    get_batch_by_name,
    init_db,
    insert_table,
    list_batches,
    list_tables,
    load_batch_tables,
    load_table,
    parse_and_preview,
)


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

        with connect(app.config["DB_PATH"]) as conn:
            row = get_batch_by_name(conn, name)
            if row is None:
                batch_id = create_batch(conn, name)
                table_id = insert_table(conn, batch_id, table, note)
                flash(
                    f"新批 {name} 已创建，档表 #{table_id} 入库：{len(table.bins)} 档、"
                    f"{table.total} 粒",
                    "ok",
                )
            else:
                batch_id = int(row["id"])
                existing = load_batch_tables(conn, batch_id)
                ok, reason = compatible(existing + [table])
                if not ok:
                    # 补报失败：不写入，后续核算仍只按此前已入库档表
                    flash(
                        f"补报被拒绝（与批 {name} 已入库的 {len(existing)} 张档表不相容）："
                        f"{reason}。本次上传未入库，核算仍按原档表进行。",
                        "error",
                    )
                    return redirect(url_for("index"))
                table_id = insert_table(conn, batch_id, table, note)
                flash(
                    f"批 {name} 补报成功：档表 #{table_id} 与已有 {len(existing)} 张档表相容，"
                    f"现共 {len(existing) + 1} 张档表、{table.total} 粒",
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

    @app.route("/compute", methods=["POST"])
    def compute():
        ids = [int(x) for x in request.form.getlist("batch_ids")]
        if not ids:
            flash("请至少勾选一个批次", "error")
            return redirect(url_for("index"))
        with connect(app.config["DB_PATH"]) as conn:
            all_batches = {int(r["id"]): dict(r) for r in list_batches(conn)}
            missing = [i for i in ids if i not in all_batches]
            if missing:
                flash(f"批次不存在：{missing}", "error")
                return redirect(url_for("index"))
            batch_tables = [(bid, load_batch_tables(conn, bid)) for bid in ids]
            if any(not ts for _, ts in batch_tables):
                flash("所选批次中存在没有任何档表的批，无法核算", "error")
                return redirect(url_for("index"))
            try:
                result = merge_p90(batch_tables)
            except BinTableError as exc:
                flash(f"核算失败：{exc}", "error")
                return redirect(url_for("index"))

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

            ctx = {
                "names": {bid: all_batches[bid]["name"] for bid in ids},
                "ids": ids,
                "total": result.total,
                "k": result.k,
                "p90_low": result.p90_low,
                "p90_high": result.p90_high,
                "witness_low": witness_rows(result.witness_low),
                "witness_high": witness_rows(result.witness_high),
            }
        return render_template("result.html", **ctx)

    return app
