# api/index.py
import ccxt
import pandas as pd
from flask import Flask, render_template, request, send_file, flash, redirect, url_for
from datetime import datetime, timedelta, timezone
import sys
import warnings
import time
import io # For sending file from memory
import os # Keep os import if needed elsewhere

# Suppress specific PerformanceWarnings from openpyxl if they appear
warnings.filterwarnings("ignore", category=UserWarning, module='openpyxl')

# --- Initialize Flask App (Simplified for Vercel) ---
# Let Flask and Vercel likely find templates/static relative to project root by default,
# even though this script is in api/
app = Flask(__name__)

# Required for flashing messages
# IMPORTANT: Generate your own random secret key for production!
# You can use Python's os.urandom(24) to generate one in a Python console.
app.secret_key = b'_CHANGE_THIS_TO_YOUR_OWN_RANDOM_BYTES_\xec]/'

# --- Data Fetching Function (fetch_crypto_ohlcv - No Changes) ---
def fetch_crypto_ohlcv(symbol, start_date_str, end_date_str, exchange_id='binance', timeframe='1d'):
    """Fetches crypto OHLCV data using ccxt. Returns DataFrame or None."""
    app.logger.info(f"Attempting fetch: {exchange_id} - {symbol} - {timeframe} - {start_date_str} to {end_date_str}")
    try:
        app.logger.info(f"Connecting to crypto exchange: {exchange_id}...")
        try:
            exchange_class = getattr(ccxt, exchange_id)
        except AttributeError:
             flash(f"Error: Exchange '{exchange_id}' is not supported by ccxt.", "error")
             app.logger.error(f"Unsupported exchange: {exchange_id}")
             return None
        exchange = exchange_class({'enableRateLimit': True, 'timeout': 30000})

        symbol_upper = symbol.upper()
        try:
            app.logger.info(f"Checking crypto market symbol {symbol_upper}...")
            test_ohlcv = exchange.fetch_ohlcv(symbol_upper, timeframe, limit=1)
            if not test_ohlcv:
                 app.logger.warning("Initial check returned no data, loading markets for validation...")
                 exchange.load_markets(reload=True)
                 if symbol_upper not in exchange.markets or not exchange.markets[symbol_upper].get('active', True):
                      raise ccxt.BadSymbol(f"Symbol '{symbol_upper}' not found or inactive after loading markets.")
                 else:
                      app.logger.info(f"Symbol {symbol_upper} found but no recent candle data in initial check. Proceeding...")
            app.logger.info(f"Symbol {symbol_upper} found and appears active on {exchange_id}.")
        except ccxt.BadSymbol as e:
             flash(f"Error: Symbol '{symbol_upper}' not found or inactive on {exchange_id}.", "error")
             app.logger.error(f"BadSymbol: {e}")
             return None
        except Exception as e:
            flash(f"Error checking crypto market {symbol_upper}: {e}", "error")
            app.logger.error(f"Error checking symbol {symbol_upper}: {e}")
            return None

        # Prepare Timestamps
        start_dt = datetime.strptime(start_date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        end_dt_filter = datetime.strptime(end_date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        since = int(start_dt.timestamp() * 1000)
        end_timestamp_ms = int((end_dt_filter + timedelta(days=1)).timestamp() * 1000)

        all_ohlcv = []
        limit = 1000
        app.logger.info(f"Fetching {timeframe} crypto OHLCV data for {symbol_upper}...")

        while since < end_timestamp_ms:
            app.logger.info(f"Fetching batch since {datetime.fromtimestamp(since / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
            try:
                ohlcv = exchange.fetch_ohlcv(symbol_upper, timeframe, since, limit)
                if not ohlcv: app.logger.info("No more data returned."); break
                all_ohlcv.extend(ohlcv)
                last_timestamp = ohlcv[-1][0]
                timeframe_duration_ms = exchange.parse_timeframe(timeframe) * 1000
                since = last_timestamp + timeframe_duration_ms
                if exchange.rateLimit: time.sleep(exchange.rateLimit / 1000)
                if last_timestamp >= end_dt_filter.timestamp() * 1000 + timeframe_duration_ms:
                    app.logger.info("Fetched past desired end date.")
                    break
            except ccxt.RateLimitExceeded as e: app.logger.warning(f"Rate limit: {e}. Retrying after 60s..."); time.sleep(60)
            except ccxt.NetworkError as e: app.logger.warning(f"Network error: {e}. Retrying after 10s..."); time.sleep(10)
            except ccxt.RequestTimeout as e: app.logger.warning(f"Request Timeout: {e}. Retrying after 30s..."); time.sleep(30)
            except ccxt.ExchangeError as e: flash(f"Exchange error during fetch: {e}. Stopping fetch.", "error"); app.logger.error(f"Exchange error: {e}"); break
            except Exception as e: flash(f"Unexpected fetch error: {e}", "error"); app.logger.error(f"Unexpected fetch error: {e}"); return None

        if not all_ohlcv: flash(f"No crypto data collected for {symbol_upper}.", "warning"); return pd.DataFrame()

        df = pd.DataFrame(all_ohlcv, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df.drop_duplicates(subset=['Timestamp'], keep='first', inplace=True)
        df['Date'] = pd.to_datetime(df['Timestamp'], unit='ms', utc=True)

        start_dt_compare = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_dt_compare = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        df = df[(df['Date'].dt.date >= start_dt_compare) & (df['Date'].dt.date <= end_dt_compare)]

        if df.empty: flash(f"No data matched the date range {start_date_str} to {end_date_str}.", "warning"); return df

        df = df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']].copy()
        df['Date'] = df['Date'].dt.strftime('%Y-%m-%d')
        df[['Open', 'High', 'Low', 'Close', 'Volume']] = df[['Open', 'High', 'Low', 'Close', 'Volume']].apply(pd.to_numeric, errors='coerce')
        df['Volume'] = df['Volume'].astype('Float64')
        df.sort_values(by='Date', inplace=True)

        app.logger.info(f"Successfully processed {len(df)} crypto data points.")
        return df
    except Exception as e:
        flash(f"Overall error in fetch_crypto_ohlcv: {e}", "error")
        app.logger.error(f"Overall error in fetch_crypto_ohlcv: {e}")
        return None

# --- Flask Routes ---

@app.route('/', methods=['GET', 'POST'])
def index():
    app.logger.info(f"Request Method: {request.method}")
    try:
        if request.method == 'POST':
            app.logger.info("Handling POST request")
            # --- Get form data ---
            exchange_id = request.form.get('exchange', 'binance').strip().lower()
            if not exchange_id: # Handles case where select was disabled and other input was used
                 exchange_id = request.form.get('exchange_other', '').strip().lower()
            if not exchange_id: # Final fallback
                 exchange_id = 'binance'
            app.logger.info(f"Selected Exchange: {exchange_id}")

            symbol = request.form.get('symbol', '').strip().upper()
            start_date_str = request.form.get('start_date', '')
            end_date_str = request.form.get('end_date', '')
            timeframe = request.form.get('timeframe', '1d').strip().lower()
            download_format = request.form.get('format', 'excel')
            app.logger.info(f"Form Data: {symbol}, {start_date_str}, {end_date_str}, {timeframe}, {download_format}")


            # --- Basic Validation ---
            errors = False
            if not exchange_id: flash("Exchange ID is required.", "error"); errors = True
            if not symbol or "/" not in symbol: flash("Symbol is required in BASE/QUOTE format (e.g., BTC/USDT).", "error"); errors = True
            if not start_date_str: flash("Start Date is required.", "error"); errors = True
            if not end_date_str: flash("End Date is required.", "error"); errors = True
            if not timeframe: flash("Timeframe is required.", "error"); errors = True
            try:
                if start_date_str and end_date_str and datetime.strptime(end_date_str, '%Y-%m-%d') < datetime.strptime(start_date_str, '%Y-%m-%d'):
                    flash("End date cannot be before start date.", "error"); errors = True
            except ValueError:
                flash("Invalid date format. Please use YYYY-MM-DD.", "error"); errors = True

            if errors:
                app.logger.warning("Form validation failed.")
                return redirect(url_for('index')) # Redirect back to form with flash messages

            # --- Fetch data ---
            df = fetch_crypto_ohlcv(symbol, start_date_str, end_date_str, exchange_id, timeframe)

            # --- Prepare and Send File ---
            if df is None:
                app.logger.error("fetch_crypto_ohlcv returned None.")
                # Error likely already flashed by fetch function
                return redirect(url_for('index'))
            elif df.empty:
                 app.logger.warning("fetch_crypto_ohlcv returned empty DataFrame.")
                 # Message likely already flashed by fetch function
                 return redirect(url_for('index'))
            else:
                try:
                    app.logger.info(f"Preparing file for download ({download_format})...")
                    safe_symbol = symbol.replace('/', '_')
                    filename = f"{safe_symbol}_{exchange_id}_{start_date_str}_to_{end_date_str}_{timeframe}.{download_format if download_format == 'txt' else 'xlsx'}"
                    output_buffer = io.BytesIO()

                    if download_format == 'excel':
                        mimetype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                        sheet_name = f"{safe_symbol}_{timeframe}"[:31]
                        with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer:
                             df.to_excel(writer, sheet_name=sheet_name, index=False)
                        output_buffer.seek(0)

                    elif download_format == 'txt':
                        mimetype = 'text/plain'
                        df.to_csv(output_buffer, sep='\t', index=False, encoding='utf-8', lineterminator='\n')
                        output_buffer.seek(0)
                    else:
                         flash("Invalid download format selected.", "error")
                         app.logger.error(f"Invalid download format: {download_format}")
                         return redirect(url_for('index'))

                    # flash(f"Data fetched successfully for {symbol}. Preparing download...", "success") # Flashing before send_file doesn't work well
                    app.logger.info(f"Sending file: {filename}, mimetype: {mimetype}")
                    return send_file(
                        output_buffer,
                        mimetype=mimetype,
                        as_attachment=True,
                        download_name=filename
                    )

                except Exception as e:
                    flash(f"Error preparing file for download: {e}", "error")
                    app.logger.error(f"Error preparing file: {e}")
                    return redirect(url_for('index'))

        # --- Handle GET request (Display Form) ---
        elif request.method == 'GET':
            app.logger.info("Handling GET request, rendering template.")
            common_exchanges = ['binance', 'kraken', 'bybit', 'coinbase', 'kucoin', 'okx', 'gateio', 'bitget']
            # Render the actual HTML page now
            return render_template('index.html', exchanges=common_exchanges)

        # Fallback for methods other than GET/POST
        else:
            app.logger.warning(f"Unhandled request method: {request.method}")
            return "Method Not Allowed", 405

    except Exception as route_exception:
        # Catch-all for any unexpected error within the route handler
        app.logger.error(f"Unhandled Exception in '/' route: {route_exception}", exc_info=True)
        flash("An unexpected server error occurred. Please check server logs.", "error")
        return redirect(url_for('index')) # Redirect home on general errors


# --- Run the App section (FOR LOCAL TESTING ONLY) ---
# if __name__ == '__main__':
#    print("--- Running Flask App Locally for Testing ---")
#    app.run(debug=True, host='0.0.0.0', port=5000)