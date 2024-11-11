import os
import shutil
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox

def get_sd_card_path():
    volumes = os.listdir('/Volumes')
    for volume in volumes:
        if volume not in ('Macintosh HD', 'Macintosh HD - Data'):  # Exclude the system's default volumes
            return os.path.join('/Volumes', volume)
    return None

def copy_photos_from_sd(sd_card_path, destination_path):
    # Define photo and video extensions
    photo_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.raw', 
                        '.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.raf', '.arw', '.crw', '.jpeg', '.jpg', '.png', '.rw2', '.orf','.nef','.nrw','.pef','.dng','.tiff', '.tif', '.cr2', '.psd', '.cr3')

    copied_files = 0
    skipped_files = 0

    # Walk through all directories and subdirectories
    for root, _, files in os.walk(sd_card_path):
        for file in files:
            if file.lower().endswith(photo_extensions):
                # Get the full path of the file
                file_path = os.path.join(root, file)
                
                # Get the modification time and convert it to a datetime object
                mod_time = os.path.getmtime(file_path)
                mod_datetime = datetime.fromtimestamp(mod_time)
                
                # Create the year/month/day folder paths
                year_folder = os.path.join(destination_path, str(mod_datetime.year))
                month_folder = os.path.join(year_folder, f"{mod_datetime.month:02}")
                date_folder = os.path.join(month_folder, f"{mod_datetime.day:02}")
                
                # Create the folders if they don't exist
                os.makedirs(date_folder, exist_ok=True)
                
                # Define the destination file path
                dest_file_path = os.path.join(date_folder, file)
                
                # Check if the file already exists in the destination
                if not os.path.exists(dest_file_path):
                    # Copy the file to the date folder
                    shutil.copy2(file_path, dest_file_path)
                    copied_files += 1
                    print(f"Copied {file} to {date_folder}")
                else:
                    skipped_files += 1
                    print(f"Skipped {file} as it already exists in {date_folder}")

    messagebox.showinfo("Photo Importer", f"Photo import process completed.\n\n"
                                          f"Files copied: {copied_files}\n"
                                          f"Files skipped: {skipped_files}")

def start_import():
    sd_card_path = sd_card_path_entry.get()
    destination_path = destination_path_entry.get()

    if not sd_card_path:
        messagebox.showerror("Photo Importer", "SD card not found.")
        return
    
    copy_photos_from_sd(sd_card_path, destination_path)

# Create the main window
root = tk.Tk()
root.title("Photo Importer")

# SD card path
sd_card_path = get_sd_card_path()

# Default destination path to Pictures folder
destination_path = os.path.join(os.path.expanduser("~"), "Pictures")

# SD card path display and entry
tk.Label(root, text="SD Card Path:").grid(row=0, column=0, padx=10, pady=5)
sd_card_path_entry = tk.Entry(root, width=50)
sd_card_path_entry.grid(row=0, column=1, padx=10, pady=5)
sd_card_path_entry.insert(0, sd_card_path if sd_card_path else "No SD card found")

# Destination path display and entry
tk.Label(root, text="Destination Path:").grid(row=1, column=0, padx=10, pady=5)
destination_path_entry = tk.Entry(root, width=50)
destination_path_entry.grid(row=1, column=1, padx=10, pady=5)
destination_path_entry.insert(0, destination_path)

# Start button
tk.Button(root, text="Start Import", command=start_import).grid(row=2, column=0, columnspan=2, padx=10, pady=20)

# Run the main loop
root.mainloop()
