import os
import shutil
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
import threading
import queue
import subprocess

def get_sd_card_path():
    volumes = os.listdir('/Volumes')
    for volume in volumes:
        if volume not in ('Macintosh HD', 'Macintosh HD - Data'):  # Exclude the system's default volumes
            return os.path.join('/Volumes', volume)
    return None

def find_NIKON_Z_6_2_card():
    """
    Find SD card named 'NIKON Z 6_2' (case-insensitive).
    Returns: path string or None
    """
    try:
        volumes = os.listdir('/Volumes')
        for volume in volumes:
            if volume.upper() == 'NIKON Z 6_2' or volume.upper() == 'RICOH GR':
                return os.path.join('/Volumes', volume)
    except Exception as e:
        print(f"Error checking volumes: {e}")
    return None

def check_sd_card_status():
    """
    Check system status of SD card insertion.
    Returns: ('status', 'path' or None)
    Status values: 'not_inserted', 'inserted_not_mounted', 'mounted', 'not_found'
    """
    try:
        # Check if NIKON Z 6_2 volume is mounted
        eos_path = find_NIKON_Z_6_2_card()
        if eos_path and os.path.exists(eos_path):
            # Verify it's actually accessible
            try:
                os.listdir(eos_path)
                return ('mounted', eos_path)
            except:
                return ('inserted_not_mounted', None)
        
        # Check if any SD card is physically inserted using diskutil
        try:
            result = subprocess.run(['diskutil', 'list'], 
                                  capture_output=True, 
                                  text=True, 
                                  timeout=5)
            if result.returncode == 0:
                # Look for external disks (SD cards typically show as external)
                output = result.stdout.lower()
                # Check for common SD card indicators
                if 'external' in output or 'disk' in output:
                    # SD card might be inserted but not mounted as NIKON Z 6_2
                    if eos_path is None:
                        return ('inserted_not_mounted', None)
        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            print(f"Error checking diskutil: {e}")
        
        return ('not_found', None)
    except Exception as e:
        print(f"Error in check_sd_card_status: {e}")
        return ('not_found', None)

def log_message(log_queue, message):
    """Add a message to the queue for thread-safe GUI updates."""
    log_queue.put(message)

def process_log_queue(root, log_widget, log_queue):
    """Process messages from the queue and update the GUI."""
    try:
        while True:
            message = log_queue.get_nowait()
            log_widget.insert(tk.END, message + "\n")
            log_widget.see(tk.END)  # Auto-scroll to bottom
    except queue.Empty:
        pass
    # Schedule this function to run again
    root.after(100, process_log_queue, root, log_widget, log_queue)

def update_status_display(status_label, status_text, color):
    """Update the status label with text and color."""
    status_label.config(text=status_text, fg=color)

def poll_for_sd_card(root, status_label, sd_card_path_entry, start_button, polling_active, log_queue):
    """
    Poll for NIKON Z 6_2 SD card every 2 seconds.
    Updates GUI status and triggers confirmation when found.
    """
    if not polling_active[0]:
        return
    
    status, path = check_sd_card_status()
    
    if status == 'mounted' and path:
        # SD card found!
        update_status_display(status_label, "SD Card Status: OK - NIKON Z 6_2 detected", "green")
        sd_card_path_entry.delete(0, tk.END)
        sd_card_path_entry.insert(0, path)
        log_message(log_queue, f"[{datetime.now().strftime('%H:%M:%S')}] NIKON Z 6_2 SD card detected: {path}")
        
        # Enable start button
        start_button.config(state=tk.NORMAL)
        
        # Auto-trigger confirmation dialog
        if not hasattr(poll_for_sd_card, 'dialog_shown') or not poll_for_sd_card.dialog_shown:
            poll_for_sd_card.dialog_shown = True
            log_message(log_queue, f"[{datetime.now().strftime('%H:%M:%S')}] Prompting for user confirmation...")
            root.after(500, lambda: confirm_and_start_import(root, path, status_label, polling_active, start_button, log_queue))
    elif status == 'inserted_not_mounted':
        update_status_display(status_label, "SD Card Status: Inserted but not mounted...", "yellow")
        sd_card_path_entry.delete(0, tk.END)
        sd_card_path_entry.insert(0, "Waiting for NIKON Z 6_2 to mount...")
        start_button.config(state=tk.DISABLED)
        log_message(log_queue, f"[{datetime.now().strftime('%H:%M:%S')}] SD card inserted but NIKON Z 6_2 not mounted yet...")
    else:
        update_status_display(status_label, "SD Card Status: Waiting for NIKON Z 6_2...", "yellow")
        sd_card_path_entry.delete(0, tk.END)
        sd_card_path_entry.insert(0, "No SD card found")
        start_button.config(state=tk.DISABLED)
        poll_for_sd_card.dialog_shown = False
        log_message(log_queue, f"[{datetime.now().strftime('%H:%M:%S')}] Checking for NIKON Z 6_2 SD card...")
    
    # Schedule next poll
    if polling_active[0]:
        root.after(2000, poll_for_sd_card, root, status_label, sd_card_path_entry, start_button, polling_active, log_queue)

def confirm_and_start_import(root, sd_card_path, status_label, polling_active, start_button, log_queue):
    """Show confirmation dialog and start import if user confirms."""
    destination_path = destination_path_entry.get()
    
    # Stop polling during import
    polling_active[0] = False
    
    # Show confirmation dialog
    response = messagebox.askyesno(
        "SD Card Detected",
        f"NIKON Z 6_2 SD card detected!\n\n"
        f"SD Card: {sd_card_path}\n"
        f"Destination: {destination_path}\n\n"
        f"Start importing photos now?",
        icon='question'
    )
    
    if response:
        # User confirmed, start import
        update_status_display(status_label, "SD Card Status: Importing...", "green")
        log_message(log_queue, f"[{datetime.now().strftime('%H:%M:%S')}] User confirmed. Starting import...")
        start_import_internal(sd_card_path, destination_path, log_queue, root, status_label)
    else:
        # User cancelled, resume polling
        polling_active[0] = True
        update_status_display(status_label, "SD Card Status: OK - Waiting for confirmation...", "green")
        log_message(log_queue, f"[{datetime.now().strftime('%H:%M:%S')}] User cancelled. Resuming detection...")
        root.after(2000, poll_for_sd_card, root, status_label, sd_card_path_entry, start_button, polling_active, log_queue)

def copy_photos_from_sd(sd_card_path, destination_path, log_queue, root, status_label):
    # Define photo and video extensions
    photo_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.raw', 
                        '.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.raf', '.arw', '.crw', '.jpeg', '.jpg', '.png', '.rw2', '.orf','.nef','.nrw','.pef','.dng','.tiff', '.tif', '.cr2', '.psd', '.cr3')

    copied_files = 0
    skipped_files = 0

    try:
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_message(log_queue, f"[{timestamp}] Starting import from: {sd_card_path}")
        log_message(log_queue, f"[{timestamp}] Destination: {destination_path}")
        log_message(log_queue, f"[{timestamp}] Scanning for photos and videos...")

        # Walk through all directories and subdirectories
        for root_dir, _, files in os.walk(sd_card_path):
            for file in files:
                if file.lower().endswith(photo_extensions):
                    # Get the full path of the file
                    file_path = os.path.join(root_dir, file)
                    
                    try:
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
                            timestamp = datetime.now().strftime('%H:%M:%S')
                            log_message(log_queue, f"[{timestamp}] Copied {file} to {date_folder}")
                            print(f"Copied {file} to {date_folder}")
                        else:
                            skipped_files += 1
                            timestamp = datetime.now().strftime('%H:%M:%S')
                            log_message(log_queue, f"[{timestamp}] Skipped {file} (already exists in {date_folder})")
                            print(f"Skipped {file} as it already exists in {date_folder}")
                    except Exception as e:
                        timestamp = datetime.now().strftime('%H:%M:%S')
                        log_message(log_queue, f"[{timestamp}] Error processing {file}: {e}")
                        print(f"Error processing {file}: {e}")

        timestamp = datetime.now().strftime('%H:%M:%S')
        log_message(log_queue, f"\n[{timestamp}] {'='*60}")
        log_message(log_queue, f"[{timestamp}] Import completed successfully!")
        log_message(log_queue, f"[{timestamp}] Files copied: {copied_files}")
        log_message(log_queue, f"[{timestamp}] Files skipped: {skipped_files}")
        log_message(log_queue, f"[{timestamp}] {'='*60}")
        
        # Update status to done (green)
        root.after(0, update_status_display, status_label, "SD Card Status: Import completed successfully!", "green")
        
        # Eject SD card after import
        try:
            subprocess.run(["diskutil", "eject", sd_card_path], check=True)
            timestamp = datetime.now().strftime('%H:%M:%S')
            log_message(log_queue, f"\n[{timestamp}] SD card ejected: {sd_card_path}")
            print(f"Ejected SD card: {sd_card_path}")
        except Exception as e:
            timestamp = datetime.now().strftime('%H:%M:%S')
            log_message(log_queue, f"\n[{timestamp}] Failed to eject SD card: {e}")
            print(f"Failed to eject SD card: {e}")
            # Ejection failure is not critical, so we don't change status to red
            
    except Exception as e:
        # Import failed - update status to red
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_message(log_queue, f"\n[{timestamp}] {'='*60}")
        log_message(log_queue, f"[{timestamp}] Import FAILED: {e}")
        log_message(log_queue, f"[{timestamp}] {'='*60}")
        root.after(0, update_status_display, status_label, f"SD Card Status: Import failed - {str(e)[:50]}", "red")

def make_readonly(entry):
    """Make an Entry widget read-only by preventing all editing events."""
    def ignore_edit(event):
        return "break"
    entry.bind('<KeyPress>', ignore_edit)
    entry.bind('<KeyRelease>', ignore_edit)
    entry.bind('<Button-1>', ignore_edit)
    entry.bind('<Button-2>', ignore_edit)
    entry.bind('<Button-3>', ignore_edit)
    entry.bind('<Double-Button-1>', ignore_edit)
    entry.bind('<Triple-Button-1>', ignore_edit)

def start_import():
    """Start import manually (called by button click)."""
    global log_queue
    sd_card_path = sd_card_path_entry.get()
    destination_path = destination_path_entry.get()

    if not sd_card_path or sd_card_path == "No SD card found" or sd_card_path == "Waiting for NIKON Z 6_2 to mount...":
        messagebox.showerror("Photo Importer", "SD card not found.")
        return
    
    # Verify card still exists
    if not os.path.exists(sd_card_path):
        messagebox.showerror("Photo Importer", "SD card path no longer accessible.")
        return
    
    # Use the global log queue
    start_import_internal(sd_card_path, destination_path, log_queue, root, status_label)

def start_import_internal(sd_card_path, destination_path, log_queue, root, status_label):
    """Internal function to start the import process."""
    # Clear the log
    log_text.delete(1.0, tk.END)
    log_message(log_queue, f"[{datetime.now().strftime('%H:%M:%S')}] ========== Starting Import ==========")
    
    # Update status to importing (green)
    update_status_display(status_label, "SD Card Status: Importing...", "green")
    
    # Run import in a separate thread
    import_thread = threading.Thread(target=copy_photos_from_sd, 
                                     args=(sd_card_path, destination_path, log_queue, root, status_label),
                                     daemon=True)
    import_thread.start()

# Create the main window
root = tk.Tk()
root.title("Photo Importer - Auto-Detection")

# Default destination path to Pictures folder
destination_path = os.path.join(os.path.expanduser("~"), "Pictures")

# Status display
status_label = tk.Label(root, text="SD Card Status: Initializing...", font=("Arial", 10, "bold"), fg="yellow")
status_label.grid(row=0, column=0, columnspan=2, padx=10, pady=5, sticky='w')

# SD card path display and entry
tk.Label(root, text="SD Card Path:").grid(row=1, column=0, padx=10, pady=5)
sd_card_path_entry = tk.Entry(root, width=50)
sd_card_path_entry.grid(row=1, column=1, padx=10, pady=5)
sd_card_path_entry.insert(0, "Waiting for NIKON Z 6_2...")
make_readonly(sd_card_path_entry)

# Destination path display and entry
tk.Label(root, text="Destination Path:").grid(row=2, column=0, padx=10, pady=5)
destination_path_entry = tk.Entry(root, width=50)
destination_path_entry.grid(row=2, column=1, padx=10, pady=5)
destination_path_entry.insert(0, destination_path)
make_readonly(destination_path_entry)

# Start button (disabled until card is detected)
start_button = tk.Button(root, text="Start Import", command=start_import, state=tk.DISABLED)
start_button.grid(row=3, column=0, columnspan=2, padx=10, pady=10)

# Log display
tk.Label(root, text="Import Log:").grid(row=4, column=0, padx=10, pady=(10, 5), sticky='nw')
log_text = scrolledtext.ScrolledText(root, width=70, height=15, wrap=tk.WORD)
log_text.grid(row=4, column=1, padx=10, pady=(10, 10), sticky='nsew')

# Configure grid weights for resizing
root.grid_rowconfigure(4, weight=1)
root.grid_columnconfigure(1, weight=1)

# Initialize global log queue and start processing immediately
log_queue = queue.Queue()
process_log_queue(root, log_text, log_queue)

# Initialize polling
polling_active = [True]  # Use list to allow modification in nested functions
poll_for_sd_card.dialog_shown = False  # Track if confirmation dialog has been shown

# Start polling for SD card
log_message(log_queue, f"[{datetime.now().strftime('%H:%M:%S')}] Auto-detection started. Looking for NIKON Z 6_2 SD card...")
log_message(log_queue, f"[{datetime.now().strftime('%H:%M:%S')}] The app will automatically detect the card and prompt for confirmation.")
log_message(log_queue, "")
root.after(1000, poll_for_sd_card, root, status_label, sd_card_path_entry, start_button, polling_active, log_queue)

# Run the main loop
root.mainloop()
