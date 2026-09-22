# Official AWS Lambda Python 3.12 base image
FROM public.ecr.aws/lambda/python:3.12

# Install Ghostscript
RUN dnf install -y ghostscript \
    && dnf clean all

# Copy Python dependencies
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy Lambda source code
COPY src/ ${LAMBDA_TASK_ROOT}/

# Lambda entry point
CMD ["app.lambda_handler"]
