#!/usr/bin/env python
"""
Backup скрипт для сохранения залогиненных профилей Chrome.

Копирует актуальные профили из work/ обратно в profiles/ (seed).
Это нужно чтобы сохранить состояние логина после длительной работы прокси.

Примеры:
  python scripts/backup_profiles.py                    # бэкапить все профили с подтверждением
  python scripts/backup_profiles.py --yes               # пропустить подтверждение
  python scripts/backup_profiles.py --profile "Profile 2"  # только один профиль
  python scripts/backup_profiles.py --profile "Profile 2" --profile "Profile 4" --yes
"""

import os
import sys
import shutil
import argparse

# Импорт конфига из родительского каталога
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import PROFILES, PROFILES_DIR, WORK_DIR


def get_dir_size_mb(path):
    """Получить размер директории в МБ."""
    total = 0
    try:
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                total += os.path.getsize(os.path.join(dirpath, f))
    except (OSError, IOError):
        return 0
    return total / (1024 * 1024)


def check_cookies(profile_name):
    """Проверить наличие и размер файла cookies."""
    cookies_path = os.path.join(WORK_DIR, profile_name, "Default", "Network", "Cookies")

    if not os.path.exists(cookies_path):
        return False

    try:
        size = os.path.getsize(cookies_path)
        return size > 0
    except OSError:
        return False


def check_file_locked(profile_name):
    """Проверить что файлы не залочены (Chrome не использует)."""
    cookies_path = os.path.join(WORK_DIR, profile_name, "Default", "Network", "Cookies")

    if not os.path.exists(cookies_path):
        return False

    try:
        # Попытка прочитать файл — если получим PermissionError, значит залочен
        with open(cookies_path, 'rb') as f:
            f.read(1)
        return False
    except PermissionError:
        return True
    except (OSError, IOError):
        return False


def backup_profiles(profile_names, skip_confirm=False):
    """
    Главная функция бэкапа.

    Args:
        profile_names: list[str] имён профилей для бэкапа
        skip_confirm: bool флаг пропуска подтверждения

    Returns:
        int код выхода (0 успех, 1 ошибка)
    """

    # Фильтр: оставить только существующие в work/
    valid_profiles = []
    for profile_name in profile_names:
        work_profile_path = os.path.join(WORK_DIR, profile_name)
        if not os.path.exists(work_profile_path):
            print(f"[skip] {profile_name}: нет work/{profile_name}, пропускаю")
            continue
        valid_profiles.append(profile_name)

    if not valid_profiles:
        print("Нечего бэкапить.")
        return 0

    # Проверка cookies
    for profile_name in valid_profiles:
        if not check_cookies(profile_name):
            print(f"[warn] {profile_name}: cookies пустые/отсутствуют — Chrome возможно не залогинен. "
                  f"Бэкап всё равно сделаем по запросу.")

    # Проверка локированных файлов
    for profile_name in valid_profiles:
        if check_file_locked(profile_name):
            print(f"[stop] {profile_name}: файлы залочены (Chrome запущен?). "
                  f"Останови сервер перед бэкапом!")
            return 1

    # Подтверждение
    if not skip_confirm:
        print(f"\nБудут перезаписаны {len(valid_profiles)} профил(ей) в {PROFILES_DIR}:")
        for profile_name in valid_profiles:
            print(f"  work/{profile_name} → profiles/{profile_name}")

        answer = input("\nПродолжить? [y/N]: ").strip().lower()
        if answer not in ('y', 'yes'):
            print("Отмена.")
            return 0

    # Копирование
    print()
    for profile_name in valid_profiles:
        work_path = os.path.join(WORK_DIR, profile_name)
        profiles_path = os.path.join(PROFILES_DIR, profile_name)

        try:
            # Удалить старый профиль если существует
            if os.path.exists(profiles_path):
                shutil.rmtree(profiles_path)

            # Копировать новый
            shutil.copytree(work_path, profiles_path)

            size_mb = get_dir_size_mb(profiles_path)
            print(f"[{profile_name}] копирую -> done ({size_mb:.1f} MB)")
        except (OSError, shutil.Error) as e:
            print(f"[{profile_name}] ошибка: {e}")
            return 1

    print(f"\nГотово. profiles/ обновлены: {', '.join(valid_profiles)}.")
    return 0


def main():
    """Точка входа."""
    # Установить UTF-8 для stdout в Windows
    if sys.stdout.encoding and 'utf' not in sys.stdout.encoding.lower():
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    parser = argparse.ArgumentParser(
        description="Backup залогиненных профилей Chrome из work/ в profiles/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Пропустить подтверждение"
    )
    parser.add_argument(
        "--profile",
        action="append",
        dest="profile_names",
        help="Бэкапить только указанный профиль (можно несколько раз)"
    )

    args = parser.parse_args()

    # Если профили не указаны — бэкапить все из конфига
    profile_names = args.profile_names if args.profile_names else PROFILES

    sys.exit(backup_profiles(profile_names, skip_confirm=args.yes))


if __name__ == "__main__":
    main()
