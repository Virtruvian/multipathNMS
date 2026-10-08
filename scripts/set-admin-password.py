#!/usr/bin/env python3
import argparse
import getpass
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.auth_credentials import write_credentials


def main() -> None:
    parser = argparse.ArgumentParser(description='Create or reset one multipathNMS administrator; keep other accounts')
    parser.add_argument('--file', type=Path, default=ROOT / 'data' / 'admin-auth.json')
    args = parser.parse_args()
    username = input('Gebruikersnaam [admin]: ').strip() or 'admin'
    password = getpass.getpass('Wachtwoord (minimaal 12 tekens): ')
    if password != getpass.getpass('Herhaal wachtwoord: '):
        raise ValueError('De wachtwoorden zijn niet gelijk')
    write_credentials(args.file, username, password)
    print(f'Adminlogin ingesteld voor {username}. Andere accounts blijven bewaard. Een draaiende container gebruikt de wijziging direct.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, EOFError, KeyboardInterrupt) as exc:
        print(str(exc) or 'Afgebroken', file=sys.stderr)
        sys.exit(1)
