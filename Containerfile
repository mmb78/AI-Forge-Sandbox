FROM ghcr.io/prefix-dev/pixi:latest

# Create a non-root user
RUN useradd -m -s /bin/bash agent

WORKDIR /app

# Disable the FastMCP ASCII Banner ---
ENV FASTMCP_SHOW_SERVER_BANNER=0

# Initialize project and dependencies
RUN pixi init && \
    pixi add python openai mcp fastmcp

COPY god_tools.py config.py ./
RUN mkdir /app/workspace

# Change ownership of the app directory to the new user
RUN chown -R agent:agent /app

# Switch to the non-root user
USER agent