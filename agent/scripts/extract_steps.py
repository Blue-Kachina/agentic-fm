#!/usr/bin/env python3
"""
Extract individual script steps from a FileMaker script XML file.

This script reads a FileMaker script XML file and creates individual XML files
for each step, organized in a folder named after the script.
"""

import xml.etree.ElementTree as ET
import os
import sys
from pathlib import Path


def sanitize_filename(name):
    """
    Sanitize a filename by replacing invalid characters.
    
    Args:
        name: The original filename
        
    Returns:
        A sanitized filename safe for filesystem use
    """
    # Replace invalid characters with underscores or remove them
    invalid_chars = '<>:"/\\|?*'
    sanitized = name
    for char in invalid_chars:
        sanitized = sanitized.replace(char, '_')
    
    # Remove leading/trailing spaces and dots
    sanitized = sanitized.strip('. ')
    
    # Replace multiple spaces with single space
    sanitized = ' '.join(sanitized.split())
    
    return sanitized


def create_step_xml(step_element):
    """
    Create a complete XML document for a single step.
    
    Args:
        step_element: The Step XML element
        
    Returns:
        A formatted XML string
    """
    # Create the root element
    root = ET.Element('fmxmlsnippet', type='FMObjectList')
    
    # Copy the step element and all its children
    root.append(step_element)
    
    # Create XML declaration and format
    tree = ET.ElementTree(root)
    ET.indent(tree, space='  ')
    
    # Convert to string with XML declaration
    xml_str = '<?xml version="1.0"?>\n'
    xml_str += ET.tostring(root, encoding='unicode')
    
    return xml_str


def extract_steps(input_file, output_base_dir=None):
    """
    Extract steps from a FileMaker script XML file.
    
    Args:
        input_file: Path to the input script.xml file
        output_base_dir: Base directory for output (defaults to same as input file)
    """
    # Parse the XML file
    try:
        input_path = Path(input_file).resolve()
        
        # Read the file first to handle any encoding issues
        with open(input_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Remove BOM if present
        if content.startswith('\ufeff'):
            content = content[1:]
        
        root = ET.fromstring(content)
    except ET.ParseError as e:
        print(f"Error parsing XML file: {e}")
        # Try to show first few characters for debugging
        try:
            with open(input_file, 'rb') as f:
                first_bytes = f.read(100)
                print(f"First bytes of file: {first_bytes}")
        except:
            pass
        sys.exit(1)
    except FileNotFoundError:
        print(f"Error: File '{input_file}' not found")
        sys.exit(1)
    
    # Find the Script element
    script = root.find('Script')
    if script is None:
        print("Error: No Script element found in the XML file")
        sys.exit(1)
    
    # Get the script name for the folder
    script_name = script.get('name')
    if not script_name:
        print("Error: Script element has no 'name' attribute")
        sys.exit(1)
    
    print(f"Extracting steps from script: '{script_name}'")
    
    # Determine output directory
    if output_base_dir is None:
        output_base_dir = Path(input_file).parent
    else:
        output_base_dir = Path(output_base_dir)
    
    # Create output folder based on script name
    output_dir = output_base_dir / sanitize_filename(script_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {output_dir}")
    
    # Find all Step elements
    steps = script.findall('Step')
    if not steps:
        print("Warning: No Step elements found in the script")
        return
    
    print(f"Found {len(steps)} steps")
    
    # Process each step
    for idx, step in enumerate(steps, 1):
        step_name = step.get('name')
        if not step_name:
            print(f"Warning: Step {idx} has no 'name' attribute, skipping")
            continue
        
        # Create a copy of the step to avoid modifying the original
        step_copy = ET.fromstring(ET.tostring(step))
        
        # Generate XML content
        xml_content = create_step_xml(step_copy)
        
        # Create filename from step name
        filename = sanitize_filename(step_name) + '.xml'
        output_path = output_dir / filename
        
        # Write the file
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(xml_content)
            print(f"  [{idx}/{len(steps)}] Created: {filename}")
        except IOError as e:
            print(f"  Error writing file '{filename}': {e}")
    
    print(f"\nSuccessfully extracted {len(steps)} steps to '{output_dir}'")


def main():
    """Main entry point for the script."""
    if len(sys.argv) < 2:
        print("Usage: python extract_steps.py <script.xml> [output_directory]")
        print("\nExtracts individual script steps from a FileMaker script XML file.")
        print("Each step is saved as a separate XML file in a folder named after the script.")
        sys.exit(1)
    
    input_file = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None
    
    extract_steps(input_file, output_dir)


if __name__ == '__main__':
    # Windows pipes and redirects default to the ANSI code page: accented names would garble
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    main()
