import os
import sys

def process_files(root_dir, output_file):
    """
    Recursively processes all files in the given directory and its subdirectories,
    writing each file's name and content to the output file.
    """
    try:
        with open(output_file, 'w', encoding='utf-8') as outfile:
            for root, dirs, files in os.walk(root_dir):
                for filename in files:
                    filepath = os.path.join(root, filename)
                    
                    # Skip the output file itself to avoid recursion
                    if filepath == os.path.abspath(output_file):
                        continue
                    
                    # Write the file name header
                    outfile.write(f"=== FILE: {filepath} ===\n")
                    
                    try:
                        # Try to read the file as text
                        with open(filepath, 'r', encoding='utf-8') as infile:
                            content = infile.read()
                            outfile.write(content)
                    except UnicodeDecodeError:
                        # If it's a binary file, note that instead
                        outfile.write("[BINARY FILE - CONTENT NOT SHOWN]\n")
                    except Exception as e:
                        # Handle other potential errors
                        outfile.write(f"[ERROR READING FILE: {str(e)}]\n")
                    
                    # Add separation between files
                    outfile.write("\n" + "="*80 + "\n\n")
        
        print(f"Successfully processed all files. Output saved to: {output_file}")
        
    except Exception as e:
        print(f"An error occurred: {str(e)}")

if __name__ == "__main__":
    # Get the directory to process (default to current directory)
    target_dir = input("Enter the directory to process (press Enter for current directory): ").strip()
    if not target_dir:
        target_dir = "."
    
    # Get the output file name
    output_filename = input("Enter the output filename (default: all_files.txt): ").strip()
    if not output_filename:
        output_filename = "all_files.txt"
    
    # Process the files
    process_files(target_dir, output_filename)