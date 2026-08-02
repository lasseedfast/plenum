import requests
from bs4 import BeautifulSoup
from io import BytesIO
from urllib.request import urlopen
from zipfile import ZipFile
import os
from time import sleep
import talks2db


def download(all=False, year=None):
    if all:
        for year in range(1999, 2026):
            first_part = str(year)
            second_part = str(year + 1)[2:]
            if first_part == '1999':
                 url = 'https://data.riksdagen.se/dataset/anforande/anforande-19992000.json.zip'
            else:
                url = f"https://data.riksdagen.se/dataset/anforande/anforande-{first_part}{second_part}.json.zip"
            print(url)

            # Ensure the 'talks' directory exists
            talks_dir = "talks"
            os.makedirs(talks_dir, exist_ok=True)

            # Create a subdirectory for the current year range
            dir_name = os.path.join(talks_dir, f"anforande-20{first_part}{second_part}")
            if os.path.exists(dir_name) and os.listdir(dir_name):
                print(f"Skipping {dir_name}, already exists and is not empty.")
                continue

            os.makedirs(dir_name, exist_ok=True)

            # Download and extract the zip file directly into the subdirectory
            with urlopen(url) as zipresp:
                with ZipFile(BytesIO(zipresp.read())) as zfile:
                    zfile.extractall(dir_name)
    elif year:
        first_part = str(year)
        second_part = str(year + 1)[2:]
        url = f"https://data.riksdagen.se/dataset/anforande/anforande-{first_part}{second_part}.json.zip"
        print(url)

        # Ensure the 'talks' directory exists
        talks_dir = "talks"
        os.makedirs(talks_dir, exist_ok=True)

        # Create a subdirectory for the current year range
        dir_name = os.path.join(talks_dir, f"anforande-20{first_part}{second_part}")
        if os.path.exists(dir_name) and os.listdir(dir_name):
            print(f"Skipping {dir_name}, already exists and is not empty.")
            return

        os.makedirs(dir_name, exist_ok=True)

        # Download and extract the zip file directly into the subdirectory
        with urlopen(url) as zipresp:
            with ZipFile(BytesIO(zipresp.read())) as zfile:
                zfile.extractall(dir_name)

if __name__ == "__main__":
    while True:
        new_files = download()

