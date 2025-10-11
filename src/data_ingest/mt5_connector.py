import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
import logging

class MT5Connector:
    """Handles connection and data retrieval from MetaTrader 5."""
    def __init__(self, credentials):
        self._credentials = credentials
        self._connection = mt5
        self.log = logging.getLogger(self.__class__.__name__)

    def connect(self):
        """Initializes connection to the MT5 terminal."""
        self.log.info("Attempting to connect to MetaTrader 5...")
        if not self._connection.initialize(
            login=self._credentials['login'],
            password=self._credentials['password'],
            server=self._credentials['server']
        ):
            self.log.error(f"Failed to initialize MT5: {self._connection.last_error()}")
            return False
        
        self.log.info(f"Successfully connected to MT5. Version: {self._connection.version()}")
        return True

    def disconnect(self):
        """Shuts down the connection to MT5."""
        self._connection.shutdown()
        self.log.info("Disconnected from MetaTrader 5.")

    def get_connection(self):
        """Returns the raw MT5 connection object."""
        return self._connection

    def get_ohlc(self, symbol, timeframe_str, num_bars):
        """Fetches historical OHLC data."""
        try:
            timeframe = getattr(mt5, f"TIMEFRAME_{timeframe_str.upper()}")
            rates = self._connection.copy_rates_from_pos(symbol, timeframe, 0, num_bars)
            if rates is None:
                self.log.warning(f"Could not retrieve OHLC data for {symbol}: {self._connection.last_error()}")
                return None
            
            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            return df
        except Exception as e:
            self.log.error(f"Error fetching OHLC data: {e}")
            return None

    def get_tick(self, symbol):
        """Fetches the latest tick data for a symbol."""
        tick = self._connection.symbol_info_tick(symbol)
        if tick:
            return tick
        self.log.warning(f"Could not get tick for {symbol}: {self._connection.last_error()}")
        return None