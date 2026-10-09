"""VM Manager command line (run from backend/, as the account the service runs as, so the DB stays its own):

  venv/bin/python -m app.cli status                         authentication settings, who may log in
  venv/bin/python -m app.cli user show <user>                a Linux user's groups and VM Manager role
  venv/bin/python -m app.cli user check <user>               test a password through PAM (prompted, or on stdin)
  venv/bin/python -m app.cli token create --user <user> [--name NAME] [--expires-days N]
  venv/bin/python -m app.cli token list [--user <user>]
  venv/bin/python -m app.cli token revoke <id>
  venv/bin/python -m app.cli session revoke --user <user>    log a user out of every browser

Tokens are printed once (stored hashed): use them as `Authorization: Bearer <token>`
(OpenTofu: VMMANAGER_TOKEN, e2e: VMM_TOKEN).
"""
import argparse
import getpass
import sys

from app.config import settings
from app.database import SessionLocal, init_db
from app.models.auth import ApiToken
from app.services import auth_service


def _fmt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") + " UTC" if value else "-"


def cmd_status(_args) -> int:
    print(f"authentication: {'enabled' if settings.AUTH_ENABLED else 'DISABLED (AUTH_ENABLED=false)'}")
    print(f"PAM service:    {auth_service.pam_service()} (configured: {settings.AUTH_PAM_SERVICE})")
    print(f"admins:         {auth_service.service_user()} (the app's account) + groups {settings.AUTH_ADMIN_GROUPS}")
    print(f"viewers:        groups {settings.AUTH_VIEWER_GROUPS or '(none)'}")
    return 0


def cmd_user_show(args) -> int:
    try:
        groups = auth_service.user_groups(args.user)
    except KeyError:
        print(f"no such user: {args.user}", file=sys.stderr)
        return 1
    role = auth_service.role_of(args.user, cached=False)
    print(f"user:   {args.user}\ngroups: {', '.join(groups)}\nrole:   {role or 'none (cannot log in)'}")
    return 0 if role else 2


def cmd_user_check(args) -> int:
    password = getpass.getpass(f"Password for {args.user}: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    ok, reason = auth_service.check_password(args.user, password)
    role = auth_service.role_of(args.user, cached=False)
    print(f"PAM: {'ok' if ok else 'refused: ' + reason}; role: {role or 'none'}")
    return 0 if ok and role else 1


def cmd_token_create(args) -> int:
    role = auth_service.role_of(args.user, cached=False)
    if role is None:
        print(f"{args.user} can't use VM Manager (no such user, or not in groups "
              f"{settings.AUTH_ADMIN_GROUPS} / {settings.AUTH_VIEWER_GROUPS or '-'})", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        row, value = auth_service.create_token(db, args.user, args.name, args.expires_days)
    auth_service.audit_log.info(f"token created id={row.id} name={row.name!r} user={args.user} (cli)")
    print(value)
    print(f"token {row.id} '{row.name}' for {args.user} ({role})"
          f"{', expires ' + _fmt(row.expires_at) if row.expires_at else ''}: shown once, keep it safe",
          file=sys.stderr)
    return 0


def cmd_token_list(args) -> int:
    with SessionLocal() as db:
        query = db.query(ApiToken)
        if args.user:
            query = query.filter(ApiToken.username == args.user)
        rows = query.order_by(ApiToken.username, ApiToken.id).all()
    print(f"{'ID':>4}  {'USER':<16} {'NAME':<24} {'PREFIX':<13} {'CREATED':<21} {'LAST USED':<21} EXPIRES")
    for r in rows:
        print(f"{r.id:>4}  {r.username:<16} {r.name[:24]:<24} {r.prefix:<13} {_fmt(r.created_at):<21} "
              f"{_fmt(r.last_used_at):<21} {_fmt(r.expires_at)}")
    return 0


def cmd_token_revoke(args) -> int:
    with SessionLocal() as db:
        row = db.get(ApiToken, args.id)
        if row is None:
            print(f"no token {args.id}", file=sys.stderr)
            return 1
        db.delete(row)
        db.commit()
    auth_service.audit_log.info(f"token revoked id={args.id} (cli)")
    print(f"token {args.id} revoked")
    return 0


def cmd_session_revoke(args) -> int:
    with SessionLocal() as db:
        count = auth_service.delete_user_sessions(db, args.user)
    print(f"{count} session(s) of {args.user} revoked")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="VM Manager administration",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="authentication settings").set_defaults(func=cmd_status)

    user = sub.add_parser("user", help="Linux users").add_subparsers(dest="action", required=True)
    p = user.add_parser("show", help="groups and role of a user")
    p.add_argument("user")
    p.set_defaults(func=cmd_user_show)
    p = user.add_parser("check", help="test a password through PAM")
    p.add_argument("user")
    p.set_defaults(func=cmd_user_check)

    token = sub.add_parser("token", help="API tokens").add_subparsers(dest="action", required=True)
    p = token.add_parser("create", help="create a token (printed once)")
    p.add_argument("--user", required=True)
    p.add_argument("--name", default="cli")
    p.add_argument("--expires-days", type=float)
    p.set_defaults(func=cmd_token_create)
    p = token.add_parser("list", help="list tokens")
    p.add_argument("--user")
    p.set_defaults(func=cmd_token_list)
    p = token.add_parser("revoke", help="revoke a token")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_token_revoke)

    session = sub.add_parser("session", help="browser sessions").add_subparsers(dest="action", required=True)
    p = session.add_parser("revoke", help="log a user out everywhere")
    p.add_argument("--user", required=True)
    p.set_defaults(func=cmd_session_revoke)

    args = parser.parse_args(argv)
    init_db()
    return args.func(args)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
