#!/bin/bash

# Variables
SCRIPT_NAME="irc-weather-bot_v0.17_beta.py"
SCRIPT_PATH="/PATH/TO/BOT"
PYTHON_CMD="python3"
LOG_LEVEL="DEBUG"
PID_FILE="/tmp/$SCRIPT_NAME.pid"
LOG_FILE="$SCRIPT_PATH/$SCRIPT_NAME.log"
ERROR_LOG_FILE="$SCRIPT_PATH/$SCRIPT_NAME.error.log"
MAX_LOG_SIZE=1048576  # 1 MB

# Rotate logs if too large
if [ -f "$LOG_FILE" ] && [ $(stat -c%s "$LOG_FILE") -ge $MAX_LOG_SIZE ]; then
    mv "$LOG_FILE" "$LOG_FILE.bak"
    : > "$LOG_FILE"
fi

# Function to check if the bot is running
is_bot_running() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            return 0  # Bot is running
        else
            echo "$(date): Bot PID $PID not found. Removing stale PID file." >> "$LOG_FILE"
            rm -f "$PID_FILE"
            return 1
        fi
    fi
    return 1  # PID file doesn't exist
}

# Function to validate environment
validate_environment() {
    if ! command -v $PYTHON_CMD &> /dev/null; then
        echo "$(date): Python command $PYTHON_CMD not found." >> "$ERROR_LOG_FILE"
        exit 1
    fi

    if [ ! -f "$SCRIPT_PATH/$SCRIPT_NAME" ]; then
        echo "$(date): Script $SCRIPT_NAME not found in $SCRIPT_PATH." >> "$ERROR_LOG_FILE"
        exit 1
    fi
}

# Trap signals for cleanup
trap "rm -f $PID_FILE; exit" SIGINT SIGTERM

# Validate environment
validate_environment

# Start the bot if not running
if ! is_bot_running; then
    echo "$(date): $SCRIPT_NAME is not running. Starting it now..." >> "$LOG_FILE"
    cd "$SCRIPT_PATH" || exit
    $PYTHON_CMD "$SCRIPT_NAME" --log-level "$LOG_LEVEL" >> "$LOG_FILE" 2>> "$ERROR_LOG_FILE" &
    SCRIPT_PID=$!
    echo "$SCRIPT_PID" > "$PID_FILE"
    echo "$(date): $SCRIPT_NAME started with PID $SCRIPT_PID." >> "$LOG_FILE"
else
    echo "$(date): $SCRIPT_NAME is already running with PID $(cat "$PID_FILE")." >> "$LOG_FILE"
fi

