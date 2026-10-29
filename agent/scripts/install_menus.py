#!/usr/bin/env python3
"""
install_menus.py — Build and clipboard-load agentic-fm custom menu XML.

Usage:
  python agent/scripts/install_menus.py           # loads menus to clipboard
  python agent/scripts/install_menus.py --set     # loads menu set to clipboard

Reads from:
  agent/sandbox/custom_menus.xml           snapshot with catalog UUID + file header
  agent/sandbox/custom_menu_set.xml        snapshot with set catalog UUID (--set)
  agent/xml_parsed/custom_menus/{sol}/     per-menu UUIDs and IDs
  agent/xml_parsed/custom_menu_sets/{sol}/ set UUID and ID (--set)
  agent/context/{solution}/scripts.index   Agentic-fm Menu script ID

Writes:
  agent/sandbox/custom_menus.xml           ready to paste in FM (menus)
  agent/sandbox/custom_menu_set.xml        ready to paste in FM (set)

The snapshot files serve dual purpose: source of solution-specific catalog UUIDs
(not available in xml_parsed), then overwritten with the fully populated output.
Since the catalog UUID is preserved in the output, the script is idempotent.
"""

import re, os, glob, sys, subprocess, argparse

SCRIPT_NAME    = 'Agentic-fm Menu'
TEMPLATE_MENUS = 'filemaker/custom_menu/custom_menus.xml'
TEMPLATE_SET   = 'filemaker/custom_menu/custom_menu_set.xml'
SNAPSHOT_MENUS = 'agent/sandbox/custom_menus.xml'
SNAPSHOT_SET   = 'agent/sandbox/custom_menu_set.xml'
MENU_NAMES     = ['File', 'Edit', 'Selection', 'Format', 'View']


def read_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def write_file(path, content):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def extract_menus_snapshot(path):
    """Return (file_name, file_uuid, catalog_uuid) from a custom menus snapshot."""
    c = read_file(path)
    file_name = re.search(r'<FMObjectTransfer[^>]+File="([^"]+)"', c).group(1)
    file_uuid = re.search(r'<FMObjectTransfer[^>]+UUID="([^"]+)"', c).group(1)
    cat_m = re.search(
        r'<CustomMenuCatalog[^>]*>\s*<UUID[^>]*>([A-F0-9-]{36})</UUID>',
        c, re.DOTALL | re.IGNORECASE
    )
    if not cat_m:
        sys.exit(f"CustomMenuCatalog UUID not found in {path}.\n"
                 f"Recreate by copying any custom menu from FileMaker and running:\n"
                 f"  python agent/scripts/clipboard.py read {path}")
    return file_name, file_uuid, cat_m.group(1)


def extract_set_snapshot(path):
    """Return (set_catalog_uuid, set_uuid, standard_menus_uuid) from a menu set snapshot."""
    c = read_file(path)
    set_cat = re.search(
        r'<CustomMenuSetCatalog[^>]*>\s*<UUID[^>]*>([A-F0-9-]{36})</UUID>',
        c, re.DOTALL | re.IGNORECASE
    ).group(1)
    set_uuid = re.search(
        r'<CustomMenuSet[^>]*>\s*<UUID[^>]*>([A-F0-9-]{36})</UUID>',
        c, re.DOTALL | re.IGNORECASE
    ).group(1)
    std_m = re.search(r'CustomMenuSetReference[^>]+UUID="([A-F0-9-]{36})"', c, re.IGNORECASE)
    return set_cat, set_uuid, (std_m.group(1) if std_m else None)


def find_solution(base_dir, hint=None):
    """Return the solution subfolder name.

    If hint is provided (e.g. 'agentic-fm' derived from the snapshot file name),
    try to match it against available subfolders before falling back to interactive.
    """
    dirs = [d for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))]
    if not dirs:
        sys.exit(f"No solution subfolders in {base_dir}. Run Explode XML first.")
    if len(dirs) == 1:
        return dirs[0]
    # Try to match the hint (file name without extension)
    if hint:
        stem = os.path.splitext(hint)[0]
        matches = [d for d in dirs if d.lower() == stem.lower()]
        if len(matches) == 1:
            return matches[0]
    print("Multiple solutions found:")
    for i, d in enumerate(dirs, 1):
        print(f"  [{i}] {d}")
    return dirs[int(input("Which solution? ").strip()) - 1]


def read_menu_info(base_dir, solution):
    """Return {menu_name: {id, uuid}} from xml_parsed."""
    menu_dir = os.path.join(base_dir, solution)
    menus = {}
    for name in MENU_NAMES:
        files = glob.glob(os.path.join(menu_dir, f'agentic-fm \u2014 {name} - ID *.xml'))
        if not files:
            sys.exit(f"Menu not found: agentic-fm \u2014 {name}\nExpected in: {menu_dir}\n"
                     f"Run Explode XML after creating the placeholder menus in FileMaker.")
        filepath = files[0]
        menu_id = re.search(r'ID (\d+)\.xml$', filepath).group(1)
        c = read_file(filepath)
        uuid_m = re.search(r'<UUID[^>]*>([A-F0-9-]{36})</UUID>', c, re.IGNORECASE)
        if not uuid_m:
            sys.exit(f"UUID not found in {filepath}")
        menus[name] = {'id': menu_id, 'uuid': uuid_m.group(1)}
    return menus


def find_script_id(name, solution):
    index_path = f'agent/context/{solution}/scripts.index'
    with open(index_path) as f:
        for line in f:
            parts = line.strip().split('|')
            if parts and parts[0] == name:
                return parts[1]
    sys.exit(f"Script '{name}' not found in {index_path}.\n"
             f"Install the bridge script first (see filemaker/custom_menu/README.md).")


def substitute(template, tokens):
    c = template
    for k, v in tokens.items():
        c = c.replace(f'{{{{{k}}}}}', v)
    remaining = re.findall(r'\{\{[A-Z_]+\}\}', c)
    if remaining:
        sys.exit(f"Unresolved tokens: {remaining}")
    return c


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--set', action='store_true',
                        help='Load menu set to clipboard instead of individual menus')
    args = parser.parse_args()

    # Validate snapshots exist
    required = [SNAPSHOT_MENUS] + ([SNAPSHOT_SET] if args.set else [])
    for path in required:
        if not os.path.exists(path):
            kind = "menu set" if 'set' in path else "custom menus"
            sys.exit(
                f"Snapshot not found: {path}\n\n"
                f"Create it by copying the {kind} from FileMaker (Manage > Custom Menus)\n"
                f"then running:\n"
                f"  python agent/scripts/clipboard.py read {path}"
            )

    # Derive solution hint from snapshot file name header
    hint = None
    if os.path.exists(SNAPSHOT_MENUS):
        c = read_file(SNAPSHOT_MENUS)
        m = re.search(r'<FMObjectTransfer[^>]+File="([^"]+)"', c)
        if m:
            hint = m.group(1)

    solution = find_solution('agent/xml_parsed/custom_menus', hint=hint)
    print(f"Solution: {solution}")

    menus = read_menu_info('agent/xml_parsed/custom_menus', solution)
    script_id = find_script_id(SCRIPT_NAME, solution)

    print(f"Script '{SCRIPT_NAME}': id={script_id}")
    for name, info in menus.items():
        print(f"  {name}: id={info['id']}, uuid={info['uuid']}")

    # Build shared token dict
    tokens = {'SCRIPT_ID': script_id}
    for name, info in menus.items():
        tok = name.upper()
        tokens[f'MENU_{tok}_ID']   = info['id']
        tokens[f'MENU_{tok}_UUID'] = info['uuid']

    if not args.set:
        file_name, file_uuid, cat_uuid = extract_menus_snapshot(SNAPSHOT_MENUS)
        print(f"File: {file_name}  Catalog UUID: {cat_uuid}")
        tokens.update({
            'FM_FILE_NAME': file_name,
            'FM_FILE_UUID': file_uuid,
            'CATALOG_UUID': cat_uuid,
        })
        output = substitute(read_file(TEMPLATE_MENUS), tokens)
        write_file(SNAPSHOT_MENUS, output)
        print(f"Written: {SNAPSHOT_MENUS}")
        subprocess.run([sys.executable, 'agent/scripts/clipboard.py', 'write', SNAPSHOT_MENUS],
                       check=True)

    else:
        file_name, file_uuid, _ = extract_menus_snapshot(SNAPSHOT_MENUS)
        set_cat, set_uuid, std_uuid = extract_set_snapshot(SNAPSHOT_SET)

        set_dir = f'agent/xml_parsed/custom_menu_sets/{solution}'
        set_files = glob.glob(os.path.join(set_dir, 'agentic-fm - ID *.xml'))
        if not set_files:
            sys.exit(f"Menu set not found in {set_dir}. Run Explode XML first.")
        set_id = re.search(r'ID (\d+)\.xml$', set_files[0]).group(1)

        print(f"Set: id={set_id}, uuid={set_uuid}")
        tokens.update({
            'FM_FILE_NAME':       file_name,
            'FM_FILE_UUID':       file_uuid,
            'SET_CATALOG_UUID':   set_cat,
            'MENU_SET_UUID':      set_uuid,
            'MENU_SET_ID':        set_id,
            'STANDARD_MENUS_UUID': std_uuid or '00000000-0000-0000-0000-000000000000',
        })
        output = substitute(read_file(TEMPLATE_SET), tokens)
        write_file(SNAPSHOT_SET, output)
        print(f"Written: {SNAPSHOT_SET}")
        subprocess.run([sys.executable, 'agent/scripts/clipboard.py', 'write', SNAPSHOT_SET],
                       check=True)


if __name__ == '__main__':
    # Windows pipes and redirects default to the ANSI code page: accented names would garble
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    main()
