# api/index.py
import ccxt
import pandas as pd
from flask import Flask, render_template, request, send_file, flash, redirect, url_for
from datetime import datetime, timedelta, timezone
import sys
import warnings
import time
import io # For sending file from memory
import os # Import os module

# Suppress specific PerformanceWarnings from openpyxl if they appear
warnings.filterwarnings("ignore", category=UserWarning, module='openpyxl')

# --- Initialize Flask App ---

# Get the absolute path of the directory the script is in (api/)
# __file__ gives the path of the current script (api/index.py)
# Use os.path.realpath to resolve any symbolic links in the path
script_dir = os.path.dirname(os.path.realpath(__file__))

# Construct absolute paths to templates and static folders
# Assuming templates/static are one level UP from the 'api' directory
template_dir = os.path.join(script_dir, '..', 'templates')
static_dir = os.path.join(script_dir, '..', 'static')

# Use os.path.abspath to ensure we have a clean, absolute path
absolute_template_dir = os.path.abspath(template_dir)
absolute_static_dir = os.path.abspath(static_dir)

# Create the Flask app instance, explicitly providing the folder paths
app = Flask(__name__,
            template_folder=absolute_template_dir,
            static_folder=absolute_static_dir)

# Required for flashing messages
# IMPORTANT: Generate your own random secret key! Use: import os; print(os.urandom(24))
app.secret_key = b'\x92=\xe66\x95d!Y\xe3\x16\\\xd5\xbd\x17\xbf\xc4\xef\x84\xcc\x17\t\xe8z\xed' # Replace with your actual key

# --- Add Debug Logging for Paths ---
# Log the paths Flask is configured to use right after initialization
# These logs will appear in the Render deployment logs during startup.
app.logger.info(f"Initialized Flask App.")
app.logger.info(f"Script Directory (__file__): {script_dir}")
app.logger.info(f"Calculated Template Folder: {absolute_template_dir}")
app.logger.info(f"Calculated Static Folder: {absolute_static_dir}")
app.logger.info(f"Flask Instance Template Folder: {app.template_folder}")
app.logger.info(f"Flask Instance Static Folder: {app.static_folder}")


# --- Data Fetching Function (fetch_crypto_ohlcv - No Changes Needed Inside) ---
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
                      app.logger.info(f"Symbol {symbol_upper} found but no recent candle data. Proceeding...")
            app.logger.info(f"Symbol {symbol_upper} found and appears active on {exchange_id}.")
        except ccxt.BadSymbol as e:
             flash(f"Error: Symbol '{symbol_upper}' not found or inactive on {exchange_id}.", "error")
             app.logger.error(f"BadSymbol: {e}")
             return None
        except Exception as e:
            flash(f"Error checking crypto market {symbol_upper}: {e}", "error")
            app.logger.error(f"Error checking symbol {symbol_upper}: {e}")
            return None

        start_dt = datetime.strptime(start_date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        end_dt_filter = datetime.strptime(end_date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        since = int(start_dt.timestamp() * 1000)
        end_timestamp_ms = int((end_dt_filter + timedelta(days=1)).timestamp() * 1000)

        all_ohlcv = []
        limit = 1000
        app.logger.info(f"Fetching {timeframe} crypto OHLCV data for {symbol_upper}...")

        while since < end_timestamp_ms:
            app.logger.debug(f"Fetching batch since {datetime.fromtimestamp(since / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
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
            except Exception as e: flash(f"Unexpected fetch error: {e}", "error"); app.logger.error(f"Unexpected fetch error: {e}", exc_info=True); return None # Log traceback

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
        app.logger.error(f"Overall error in fetch_crypto_ohlcv: {e}", exc_info=True) # Log traceback
        return None

# --- Flask Routes ---

@app.route('/', methods=['GET', 'POST'])
def index():
    app.logger.debug(f"Request Method: {request.method} for route '/'")
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
            if not symbol or "/" not in symbol or len(symbol.split('/')) != 2 or not symbol.split('/')[0] or not symbol.split('/')[1]:
                 flash("Symbol must be in BASE/QUOTE format (e.g., BTC/USDT).", "error"); errors = True
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
                return redirect(url_for('index'))

            # --- Fetch data ---
            df = fetch_crypto_ohlcv(symbol, start_date_str, end_date_str, exchange_id, timeframe)

            # --- Prepare and Send File ---
            if df is None:
                app.logger.error("fetch_crypto_ohlcv returned None. Redirecting.")
                return redirect(url_for('index'))
            elif df.empty:
                 app.logger.warning("fetch_crypto_ohlcv returned empty DataFrame. Redirecting.")
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

                    app.logger.info(f"Sending file: {filename}, mimetype: {mimetype}")
                    return send_file(
                        output_buffer,
                        mimetype=mimetype,
                        as_attachment=True,
                        download_name=filename
                    )
                except Exception as e:
                    flash(f"Error preparing file for download: {e}", "error")
                    app.logger.error(f"Error preparing file: {e}", exc_info=True) # Log traceback
                    return redirect(url_for('index'))

        # --- Handle GET request (Display Form) ---
        elif request.method == 'GET':
            app.logger.info("Handling GET request, attempting to render template.")
            common_exchanges = ['binance', 'kraken', 'bybit', 'coinbase', 'kucoin', 'okx', 'gateio', 'bitget']
            try:
                # This is the call that was failing
                return render_template('index.html', exchanges=common_exchanges)
            except Exception as render_error:
                # Log the specific error during rendering
                app.logger.error(f"Error during render_template: {render_error}", exc_info=True)
                flash("Error loading page template. Check server logs.", "error")
                # Return a simple error message or redirect
                return "Internal Server Error: Could not load template.", 500

        # Fallback for methods other than GET/POST
        else:
            app.logger.warning(f"Unhandled request method: {request.method}")
            return "Method Not Allowed", 405

    except Exception as route_exception:
        # Catch-all for any unexpected error within the route handler
        app.logger.error(f"Unhandled Exception in '/' route: {route_exception}", exc_info=True)
        flash("An unexpected server error occurred. Please check server logs or try again later.", "error")
        return redirect(url_for('index'))


# --- Run the App section (FOR LOCAL TESTING ONLY, NOT USED BY RENDER/GUNICORN) ---
# if __name__ == '__main__':
#    print("--- Running Flask App Locally for Testing ---")
#    print(f"Script Directory: {os.path.dirname(os.path.realpath(__file__))}")
#    print(f"Template Folder Setting: {app.template_folder}")
#    print(f"Static Folder Setting: {app.static_folder}")
#    app.run(debug=True, host='0.0.0.0', port=5000)