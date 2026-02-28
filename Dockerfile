# Use a lightweight Python image
FROM python:3.9-slim

# Set the working directory
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all your game files (app.py, templates, static) into the container
COPY . .

# Expose the exact port Hugging Face looks for
EXPOSE 7860

# Start the game
CMD ["python", "app.py"]