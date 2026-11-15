#!/usr/bin/env python3
"""load_fin.py — 两代对账文件归一入库与勾稽。

老库文件:定长文本。1-14列账号,15-22列日期(YYYYMMDD),23列借贷标志(D借/C贷),
24-37列金额(单位:分),38列起凭证号。
新库文件:CSV,表头 账号,入账日,方向,金额,凭证号,余额。方向为汉字借/贷,
金额单位元、可带千分位逗号(带逗号字段以双引号包裹),个别手续费行有三位小数,
余额列每行随记(单位元,两位小数)。

归一后写入 SQLite postings 表:(voucher, account, date, direction, amount),
金额一律存分(三位小数的手续费行会出现 0.1 分,以 REAL 保存,其余为 INTEGER),
方向统一为汉字 借/贷,日期归一为 YYYY-MM-DD。

去重:切换期两代文件存在交叠,同一笔业务两边凭证号一致(金额偶有 1 分舍入差),
故以 (voucher, account, date, direction) 为业务唯一键 INSERT OR IGNORE。
先装新库文件、后装老库文件,冲突时保留新系统记录。脚本重复跑不会重复入账。

入库后两件勾稽:
1. 按账户从头复算余额(借增贷减),与新文件随记的余额列逐笔核对,
   对不上的账户写入账户登记(默认 register_account.txt)。
2. 按日合计借贷两方金额,不相等的日子写入日登记(默认 register_day.txt)。
两类登记均为文本文件,每行一记,写明标识与勾稽双方数值。

用法:
    python3 load_fin.py --old ledger_old.txt --new ledger_new.csv \
        --db fin.db [--account-register register_account.txt] \
        [--day-register register_day.txt]
"""

import argparse
import csv
import sqlite3
import sys
from decimal import Decimal

# 余额列为元、两位小数;复算保留 0.1 分精度,故容差取 0.5 分(四舍五入误差上限)。
BALANCE_TOLERANCE_FEN = Decimal("0.5") + Decimal("1e-9")

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    voucher   TEXT NOT NULL,
    account   TEXT NOT NULL,
    date      TEXT NOT NULL,            -- YYYY-MM-DD
    direction TEXT NOT NULL CHECK (direction IN ('借','贷')),
    amount    NUMERIC NOT NULL,         -- 单位:分
    UNIQUE (voucher, account, date, direction)
);
CREATE TABLE IF NOT EXISTS balances (
    -- 新库文件随记的余额,供入库后复算核对;余额单位:分
    voucher   TEXT NOT NULL,
    account   TEXT NOT NULL,
    date      TEXT NOT NULL,
    direction TEXT NOT NULL,
    amount    NUMERIC NOT NULL,
    balance   NUMERIC NOT NULL,
    seq       INTEGER NOT NULL,         -- 文件内行序,保证同日复算顺序
    UNIQUE (voucher, account, date, direction)
);
"""


def norm_date(raw):
    raw = raw.strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw  # 已是 YYYY-MM-DD 等形式,原样保留


def yuan_to_fen(text):
    """元(可带千分位逗号、可三位小数)-> 分(Decimal)。"""
    return Decimal(text.replace(",", "").strip()) * 100


def fen_for_db(fen):
    """Decimal 分 -> 入库值:整分存 INTEGER,含 0.1 分存 REAL。"""
    if fen == fen.to_integral_value():
        return int(fen)
    return float(fen)


def fen_from_db(value):
    return Decimal(str(value))


def parse_old_line(line, lineno, path):
    if len(line.rstrip("\r\n")) < 37:
        raise ValueError(f"{path}:{lineno}: 行长不足 37 列")
    account = line[0:14].strip()
    date = norm_date(line[14:22])
    flag = line[22:23]
    if flag == "D":
        direction = "借"
    elif flag == "C":
        direction = "贷"
    else:
        raise ValueError(f"{path}:{lineno}: 无法识别的借贷标志 {flag!r}")
    amount = Decimal(line[23:37].strip())  # 单位:分
    voucher = line[37:].strip()
    if not (account and voucher):
        raise ValueError(f"{path}:{lineno}: 账号或凭证号为空")
    return voucher, account, date, direction, amount


def load_old_file(conn, path):
    inserted = skipped = 0
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            voucher, account, date, direction, amount = parse_old_line(
                line, lineno, path)
            cur = conn.execute(
                "INSERT OR IGNORE INTO postings"
                " (voucher, account, date, direction, amount)"
                " VALUES (?,?,?,?,?)",
                (voucher, account, date, direction, fen_for_db(amount)))
            inserted += cur.rowcount
            skipped += 1 - cur.rowcount
    return inserted, skipped


def load_new_file(conn, path):
    inserted = skipped = 0
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for lineno, row in enumerate(reader, 2):  # 表头占第 1 行
            try:
                account = row["账号"].strip()
                date = norm_date(row["入账日"])
                direction = row["方向"].strip()
                amount = yuan_to_fen(row["金额"])
                voucher = row["凭证号"].strip()
                balance = yuan_to_fen(row["余额"])
            except (KeyError, ValueError, ArithmeticError) as e:
                raise ValueError(f"{path}:{lineno}: 解析失败: {e}") from e
            if direction not in ("借", "贷"):
                raise ValueError(f"{path}:{lineno}: 方向应为借/贷,得 {direction!r}")
            cur = conn.execute(
                "INSERT OR IGNORE INTO postings"
                " (voucher, account, date, direction, amount)"
                " VALUES (?,?,?,?,?)",
                (voucher, account, date, direction, fen_for_db(amount)))
            inserted += cur.rowcount
            skipped += 1 - cur.rowcount
            conn.execute(
                "INSERT OR IGNORE INTO balances"
                " (voucher, account, date, direction, amount, balance, seq)"
                " VALUES (?,?,?,?,?,?,?)",
                (voucher, account, date, direction, fen_for_db(amount),
                 fen_for_db(balance), lineno))
    return inserted, skipped


def signed_amount(direction, amount_fen):
    """借增贷减。"""
    return amount_fen if direction == "借" else -amount_fen


def reconcile_balances(conn, out_path):
    """按账户从头复算余额,与随记余额逐笔核对,输出账户登记。"""
    n_bad = 0
    with open(out_path, "w", encoding="utf-8") as out:
        accounts = [r[0] for r in conn.execute(
            "SELECT DISTINCT account FROM balances ORDER BY account")]
        for account in accounts:
            postings = conn.execute(
                "SELECT voucher, date, direction, amount FROM postings"
                " WHERE account=? ORDER BY date, voucher",
                (account,)).fetchall()
            recorded = conn.execute(
                "SELECT voucher, date, direction, balance FROM balances"
                " WHERE account=? ORDER BY date, voucher, seq",
                (account,)).fetchall()
            if not recorded:
                continue
            # 以首笔记账反推期初:期初 = 首笔随记余额 - 首笔发生额
            first = recorded[0]
            opening = None
            running = Decimal(0)
            recorded_map = {}
            for voucher, date, direction, balance in recorded:
                recorded_map.setdefault((voucher, date, direction),
                                        fen_from_db(balance))
            first_mismatch = None
            for voucher, date, direction, amount in postings:
                amt = fen_from_db(amount)
                key = (voucher, date, direction)
                if opening is None and key in recorded_map:
                    opening = recorded_map[key] - signed_amount(direction, amt)
                    running = opening
                if opening is None:
                    continue  # 首笔记账之前的老数据,已含在期初推导之外
                running += signed_amount(direction, amt)
                if key in recorded_map:
                    diff = running - recorded_map[key]
                    if abs(diff) > BALANCE_TOLERANCE_FEN:
                        first_mismatch = (date, voucher, running,
                                          recorded_map[key])
                        break
            if first_mismatch:
                date, voucher, computed, recorded_bal = first_mismatch
                out.write(
                    f"账号={account} 日期={date} 凭证号={voucher} "
                    f"复算余额={computed}分 随记余额={recorded_bal}分\n")
                n_bad += 1
    return n_bad


def reconcile_daily(conn, out_path):
    """按日合计借贷两方,不相等的日子输出日登记。"""
    n_bad = 0
    with open(out_path, "w", encoding="utf-8") as out:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM postings ORDER BY date")]
        for date in dates:
            debit = Decimal(0)
            credit = Decimal(0)
            for direction, amount in conn.execute(
                    "SELECT direction, amount FROM postings WHERE date=?",
                    (date,)):
                amt = fen_from_db(amount)
                if direction == "借":
                    debit += amt
                else:
                    credit += amt
            if debit != credit:
                out.write(
                    f"入账日={date} 借方合计={debit}分 贷方合计={credit}分 "
                    f"差额={debit - credit}分\n")
                n_bad += 1
    return n_bad


def main(argv=None):
    ap = argparse.ArgumentParser(description="两代对账文件归一入库并勾稽")
    ap.add_argument("--old", nargs="*", default=[], metavar="FILE",
                    help="老库定长文本文件,可多个")
    ap.add_argument("--new", nargs="*", default=[], metavar="FILE",
                    help="新库 CSV 文件,可多个")
    ap.add_argument("--db", default="fin.db", help="SQLite 库路径(默认 fin.db)")
    ap.add_argument("--account-register", default="register_account.txt",
                    help="账户登记输出路径")
    ap.add_argument("--day-register", default="register_day.txt",
                    help="日登记输出路径")
    args = ap.parse_args(argv)

    if not args.old and not args.new:
        ap.error("至少用 --old 或 --new 指定一个输入文件")

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)

    total_ins = total_skip = 0
    with conn:
        # 先装新库文件:交叠期冲突时保留新系统记录
        for path in args.new:
            ins, skip = load_new_file(conn, path)
            total_ins += ins
            total_skip += skip
            print(f"[新] {path}: 入库 {ins} 笔,重复跳过 {skip} 笔")
        for path in args.old:
            ins, skip = load_old_file(conn, path)
            total_ins += ins
            total_skip += skip
            print(f"[老] {path}: 入库 {ins} 笔,重复跳过 {skip} 笔")

    n_accounts = reconcile_balances(conn, args.account_register)
    n_days = reconcile_daily(conn, args.day_register)
    total = conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
    conn.close()

    print(f"本次入库 {total_ins} 笔,重复跳过 {total_skip} 笔;"
          f"库内累计 {total} 笔")
    print(f"账户勾稽: {n_accounts} 个账户对不上 -> {args.account_register}")
    print(f"按日勾稽: {n_days} 个日子对不上 -> {args.day_register}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
