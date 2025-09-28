# ---- Stage 1: The Builder ----
# This stage installs dependencies, including any that need to be compiled.
FROM python:3.12-alpine AS builder

# Install build-time system dependencies, including git
RUN apk add --no-cache build-base git

# Clone the repository
RUN git clone https://github.com/procrastinando/Selfhosted-Youtube-Downloader /downloader

# Set the working directory to the cloned repository
WORKDIR /downloader

# Install python dependencies from the repository's requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ---- Stage 2: The Final Image ----
# This stage creates the clean, final image for production.
FROM python:3.12-alpine

# Install runtime system dependencies. ffmpeg is needed by the bot to process audio files.
RUN apk add --no-cache ffmpeg

# Set the working directory
WORKDIR /downloader

# Copy the installed packages from the 'builder' stage
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

# Copy the application code from the 'builder' stage
COPY --from=builder /downloader .

# Set the command to run your bot
CMD ["python", "downloader.py"]