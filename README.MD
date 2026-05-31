# UdemyArchivist

Archive and download Udemy courses from the command line.


![Screenshot](https://ejemplo.com/imagen.png)
![Screenshot](https://i.imgur.com/9BV4WUI.png)

## Features

* Download enrolled courses
* MP4 and HLS support
* Download subtitles
* Download supplementary assets
* Resume interrupted downloads
* Interactive course browser
* Proxy support

## Requirements

* Python 3
* requests
* ffmpeg

Install dependencies:

```bash
pip install requests
```

Install ffmpeg from your distribution repositories.

## Authentication

Get your `access_token` cookie from Udemy:

1. Log into Udemy.
2. Open browser developer tools.
3. Find the `access_token` cookie.
4. Save it:

```bash
python udemy_archivist.py --token YOUR_TOKEN
```

## Usage

Interactive mode:

```bash
python udemy_archivist.py
```

List courses:

```bash
python udemy_archivist.py --list
```

Download a course:

```bash
python udemy_archivist.py --course 1234567
```

Select quality:

```bash
python udemy_archivist.py --course 1234567 --quality 1080
```

Skip subtitles:

```bash
python udemy_archivist.py --course 1234567 --no-subs
```

Skip supplementary assets:

```bash
python udemy_archivist.py --course 1234567 --no-assets
```

## Output

```text
downloads/
└── Course Name-1234567/
    ├── 01 - Introduction/
    │   ├── 001 - Welcome.mp4
    │   ├── 001 - Welcome.en_US.vtt
    │   └── 001 - Welcome - slides.pdf
    └── ...
```

## Disclaimer

This software is intended for downloading and archiving courses that you already have access to.

You are responsible for complying with applicable laws, copyright restrictions and Udemy's terms of service.

## License

This is free and unencumbered software released into the public domain.

See the UNLICENSE file for details.
