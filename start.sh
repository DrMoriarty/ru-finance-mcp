#!/bin/bash

MCP_TRANSPORT=streamable-http MCP_HOST=0.0.0.0 MCP_PORT=8000  python -m ru_finance.mcp_server
# эндпоинт: http://127.0.0.1:8000/mcp
