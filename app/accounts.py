from app.errors import ApiError


def create_user(conn, body) -> dict:
    try:
        row = conn.execute(
            """
            INSERT INTO users(name, email, phone, kyc_status)
            VALUES (%s, %s, %s, %s)
            RETURNING id, name, email, phone, kyc_status, created_at
            """,
            (body.name, body.email, body.phone, body.kyc_status),
        ).fetchone()
    except Exception as exc:  # unique email/phone
        if "unique" in str(exc).lower():
            raise ApiError(409, "USER_ALREADY_EXISTS", "Email or phone already registered")
        raise
    return _json_user(row)


def _json_user(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "kyc_status": row["kyc_status"],
        "created_at": str(row["created_at"]),
    }


def create_wallet(conn, user_id: int, label: str) -> dict:
    user = conn.execute("SELECT id FROM users WHERE id = %s", (user_id,)).fetchone()
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", f"User {user_id} does not exist")
    row = conn.execute(
        """
        INSERT INTO accounts(owner_user_id, kind, label, balance_paise)
        VALUES (%s, 'user_wallet', %s, 0)
        RETURNING id, owner_user_id, label, balance_paise, currency
        """,
        (user_id, label),
    ).fetchone()
    return _json_wallet(row)


def _json_wallet(row) -> dict:
    return {
        "id": row["id"],
        "owner_user_id": row["owner_user_id"],
        "label": row["label"],
        "currency": row["currency"],
        "balance_paise": row["balance_paise"],
    }


def system_account(conn, kind: str) -> int:
    row = conn.execute(
        "SELECT id FROM accounts WHERE owner_user_id IS NULL AND kind = %s",
        (kind,),
    ).fetchone()
    if row is None:
        raise ApiError(500, "SYSTEM_ACCOUNT_MISSING", f"System account {kind} is missing")
    return row["id"]


def balance(conn, user_id: int) -> dict:
    user = conn.execute("SELECT id FROM users WHERE id = %s", (user_id,)).fetchone()
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", f"User {user_id} does not exist")
    rows = conn.execute(
        """
        SELECT id, label, currency, balance_paise
        FROM accounts
        WHERE owner_user_id = %s AND kind = 'user_wallet'
        ORDER BY id
        """,
        (user_id,),
    ).fetchall()
    wallets = [
        {
            "id": r["id"],
            "label": r["label"],
            "currency": r["currency"],
            "balance_paise": r["balance_paise"],
        }
        for r in rows
    ]
    return {
        "user_id": user_id,
        "wallets": wallets,
        "total_paise": sum(w["balance_paise"] for w in wallets),
    }
