#!/usr/bin/env python3
import argparse
import getpass
import secrets
from scheduler import initialize_dashboard_auth


def main():
    parser=argparse.ArgumentParser(description='Reset Codex KVM dashboard login and invalidate all existing sessions.')
    parser.add_argument('--username',default='mark')
    parser.add_argument('--generate',action='store_true',help='Generate a strong password and print it once.')
    args=parser.parse_args()
    if args.generate:
        value=secrets.token_urlsafe(18)
    else:
        value=getpass.getpass('New dashboard password: ')
        confirm=getpass.getpass('Confirm new dashboard password: ')
        if value!=confirm:
            raise SystemExit('Passwords do not match.')
    if len(value)<12:
        raise SystemExit('Password must be at least 12 characters.')
    initialize_dashboard_auth(args.username,value,force=True)
    print(f'Codex KVM dashboard login reset for {args.username}; all previous sessions are invalid.')
    if args.generate:
        print(f'New password: {value}')


if __name__=='__main__':
    main()
