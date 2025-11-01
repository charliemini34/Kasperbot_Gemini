# src/risk/risk_manager.py

import MetaTrader5 as mt5

class RiskManager:
    def __init__(self, connector, config):
        self.connector = connector
        self.config = config
        self.risk_per_trade_pct = config.get('risk_per_trade_pct', 1.0) # 1% de risque
        self.min_stop_loss_pips = config.get('min_stop_loss_pips', 5) # 5 pips minimum

    def get_account_balance(self):
        """Récupère la balance du compte."""
        account_info = self.connector.get_account_info()
        if account_info:
            return account_info.balance
        return None

    def calculate_trade_parameters(self, symbol, entry_price, stop_loss, take_profit, trade_type, required_rr=2.0):
        """
        Calcule la taille du lot et valide le R:R.
        Si take_profit est None, le calcule basé sur required_rr.
        """
        balance = self.get_account_balance()
        if balance is None:
            print("Risk Manager: Impossible de récupérer la balance du compte.")
            return None

        # 1. Calculer le risque en $
        risk_amount = balance * (self.risk_per_trade_pct / 100.0)

        # 2. Obtenir les infos du symbole (point, tick_size, etc.)
        symbol_info = self.connector.get_symbol_info(symbol)
        if not symbol_info:
            print(f"Risk Manager: Impossible d'obtenir les infos pour {symbol}.")
            return None
        
        point = symbol_info.point
        tick_value = symbol_info.trade_tick_value
        tick_size = symbol_info.trade_tick_size
        volume_step = symbol_info.volume_step
        
        if tick_value == 0.0 or tick_size == 0.0:
            print(f"Risk Manager: Infos tick (value/size) invalides pour {symbol}.")
            return None

        # 3. Calculer la distance du SL en points (ticks)
        if trade_type == "BUY":
            sl_distance_price = entry_price - stop_loss
        elif trade_type == "SELL":
            sl_distance_price = stop_loss - entry_price
        else:
            return None # Type de trade invalide

        if sl_distance_price <= 0:
            print(f"Risk Manager: Stop Loss invalide (distance <= 0). SL: {stop_loss}, Entrée: {entry_price}")
            return None
            
        # Convertir en pips (en supposant 10 points = 1 pip, ajustez si nécessaire)
        sl_pips = (sl_distance_price / point) / 10
        if sl_pips < self.min_stop_loss_pips:
            print(f"Risk Manager: Stop Loss trop serré ({sl_pips:.1f} pips). Minimum requis: {self.min_stop_loss_pips} pips.")
            return None

        # 4. Calculer la perte par lot
        # Perte par lot = (Distance SL / Tick Size) * Tick Value
        loss_per_lot = (sl_distance_price / tick_size) * tick_value

        if loss_per_lot <= 0:
            print(f"Risk Manager: Calcul de perte par lot invalide ({loss_per_lot}).")
            return None

        # 5. Calculer la taille de lot
        lot_size = risk_amount / loss_per_lot
        
        # Arrondir au step de volume (ex: 0.01)
        lot_size = round(lot_size / volume_step) * volume_step
        
        if lot_size <= 0:
            print(f"Risk Manager: Taille de lot calculée est 0 ou négative.")
            return None

        # 6. Calculer/Valider le R:R
        
        if take_profit is None:
            # --- Logique Phase 4: Calculer le TP basé sur le R:R fixe ---
            if trade_type == "BUY":
                tp_distance_price = sl_distance_price * required_rr
                take_profit = entry_price + tp_distance_price
            elif trade_type == "SELL":
                tp_distance_price = sl_distance_price * required_rr
                take_profit = entry_price - tp_distance_price
            
            calculated_rr = required_rr
            
        else:
            # --- Logique Phase 3: Valider le R:R basé sur le TP existant ---
            if trade_type == "BUY":
                tp_distance_price = take_profit - entry_price
            elif trade_type == "SELL":
                tp_distance_price = entry_price - take_profit
            
            if tp_distance_price <= 0:
                print("Risk Manager: Take Profit invalide (distance <= 0).")
                return None
            
            calculated_rr = tp_distance_price / sl_distance_price
            
            if calculated_rr < required_rr:
                print(f"Risk Manager: R:R insuffisant ({calculated_rr:.2f}). Requis: {required_rr}.")
                return None
        
        return {
            "lot_size": lot_size,
            "stop_loss_pips": sl_pips,
            "risk_amount": risk_amount,
            "rr": calculated_rr,
            "take_profit": take_profit # Renvoyer le TP (qu'il soit calculé ou validé)
        }