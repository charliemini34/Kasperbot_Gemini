# Fichier: src/risk/risk_manager.py

import MetaTrader5 as mt5
import logging
import math

class RiskManager:
    """
    Gère le risque des trades avec calcul de lot, SL/TP,
    et une logique de breakeven et trailing stop robuste.
    Version 3.4 : Le Breakeven prend en compte le spread.
    """
    def __init__(self, config: dict, executor, symbol: str):
        self._config = config
        self._executor = executor
        self._symbol = symbol
        self.log = logging.getLogger(self.__class__.__name__)
        
        self.symbol_info = self._executor._mt5.symbol_info(self._symbol)
        self.account_info = self._executor._mt5.account_info()
        
        if not self.symbol_info or not self.account_info:
            self.log.critical("Impossible d'obtenir les infos du symbole ou du compte.")
            raise ValueError("Erreur d'initialisation du RiskManager.")
            
        self.point = self.symbol_info.point

    def calculate_volume(self, equity: float, entry_price: float, sl_price: float) -> float:
        """
        Calcule la taille de la position avec une conversion de devise explicite.
        """
        self.log.info("--- DÉBUT DU CALCUL DE VOLUME SÉCURISÉ ---")
        risk_percent = self._config.get('risk_per_trade', 0.01)
        risk_amount_account_currency = equity * risk_percent
        self.log.info(f"1. Capital: {equity:.2f} | Risque: {risk_percent*100:.2f}% -> Montant: {risk_amount_account_currency:.2f} {self.account_info.currency}")

        sl_distance_price = abs(entry_price - sl_price)
        if sl_distance_price < self.point * 10:
            self.log.error("Distance SL trop faible pour un calcul de volume. Annulation.")
            return 0.0
        self.log.info(f"2. Distance SL: {sl_distance_price:.5f}")

        contract_size = self.symbol_info.trade_contract_size
        profit_currency = self.symbol_info.currency_profit
        loss_per_lot_profit_currency = sl_distance_price * contract_size
        self.log.info(f"3. Perte/Lot en devise de profit ({profit_currency}): {loss_per_lot_profit_currency:.2f}")

        account_currency = self.account_info.currency
        loss_per_lot_account_currency = loss_per_lot_profit_currency

        if profit_currency != account_currency:
            conversion_rate = self.get_conversion_rate(profit_currency, account_currency)
            if not conversion_rate or conversion_rate == 0:
                self.log.error("Impossible d'obtenir un taux de conversion valide. Annulation.")
                return 0.0
            
            loss_per_lot_account_currency /= conversion_rate
            self.log.info(f"4. Perte/Lot convertie en {account_currency}: {loss_per_lot_account_currency:.2f}")
        else:
            self.log.info("4. Pas de conversion de devise nécessaire.")

        if loss_per_lot_account_currency <= 0:
            self.log.error("La perte par lot calculée est nulle ou négative. Annulation.")
            return 0.0
            
        volume = risk_amount_account_currency / loss_per_lot_account_currency
        self.log.info(f"5. Volume brut calculé: {volume:.4f} lots")
        
        volume_step = self.symbol_info.volume_step
        volume = math.floor(volume / volume_step) * volume_step
        volume = max(self.symbol_info.volume_min, volume)
        volume = min(self.symbol_info.volume_max, volume)
        
        final_volume = round(volume, 2)
        self.log.info(f"6. Volume final ajusté: {final_volume:.2f} lots")
        self.log.info("--- FIN DU CALCUL DE VOLUME ---")
        
        return final_volume

    def get_conversion_rate(self, from_currency: str, to_currency: str) -> float | None:
        """Trouve le taux de change pour convertir une devise en une autre."""
        pair1 = f"{to_currency}{from_currency}"
        info1 = self._executor._mt5.symbol_info_tick(pair1)
        if info1 and info1.bid > 0:
            return info1.bid

        pair2 = f"{from_currency}{to_currency}"
        info2 = self._executor._mt5.symbol_info_tick(pair2)
        if info2 and info2.ask > 0:
            return 1.0 / info2.ask
        
        self.log.error(f"Impossible de trouver un taux de change pour {from_currency} -> {to_currency}")
        return None

    def calculate_sl_tp(self, price: float, direction: str) -> tuple[float, float]:
        """Calcule les prix SL et TP en se basant sur les pips définis dans la config."""
        sl_pips = self._config.get('stop_loss_pips', 150)
        tp_pips = self._config.get('take_profit_pips', 400)
        
        sl_distance = sl_pips * 10 * self.point
        tp_distance = tp_pips * 10 * self.point

        if direction == "BUY":
            sl = price - sl_distance
            tp = price + tp_distance
        else: # SELL
            sl = price + sl_distance
            tp = price - tp_distance
            
        return round(sl, self.symbol_info.digits), round(tp, self.symbol_info.digits)

    def is_daily_loss_limit_reached(self, equity: float, daily_pnl: float) -> bool:
        """Vérifie si la limite de perte journalière est atteinte."""
        loss_limit_percent = self._config.get('daily_loss_limit_percent', 0.05)
        loss_limit_amount = equity * loss_limit_percent
        return daily_pnl < 0 and abs(daily_pnl) >= loss_limit_amount

    def manage_open_positions(self, positions: list, current_tick):
        """Orchestre la gestion des positions ouvertes (BE, Trailing)."""
        if not positions or not current_tick: return
        self._apply_breakeven(positions, current_tick)
        self._apply_trailing_stop(positions, current_tick)

    def _apply_breakeven(self, positions, tick):
        """Met le stop loss à l'équilibre en couvrant le spread."""
        cfg = self._config.get('breakeven', {})
        if not cfg.get('enabled', False): return
        
        trigger_distance = cfg.get('trigger_pips', 150) * 10 * self.point
        # Pips de sécurité pour couvrir spread/commission
        pips_plus = cfg.get('pips_plus', 10) * 10 * self.point

        for pos in positions:
            # Calcule le niveau de SL cible pour le breakeven
            if pos.type == mt5.ORDER_TYPE_BUY:
                breakeven_sl = pos.price_open + pips_plus
                # Ne rien faire si le SL est déjà au-dessus du prix d'entrée
                if pos.sl >= pos.price_open: continue
                # Vérifier si le prix actuel déclenche le BE
                if (tick.bid - pos.price_open) >= trigger_distance:
                    self.log.info(f"BREAK-EVEN+ SÉCURISÉ: Déplacement du SL à {breakeven_sl:.5f} pour #{pos.ticket}")
                    self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)
            
            elif pos.type == mt5.ORDER_TYPE_SELL:
                breakeven_sl = pos.price_open - pips_plus
                if pos.sl != 0 and pos.sl <= pos.price_open: continue
                if (pos.price_open - tick.ask) >= trigger_distance:
                    self.log.info(f"BREAK-EVEN+ SÉCURISÉ: Déplacement du SL à {breakeven_sl:.5f} pour #{pos.ticket}")
                    self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)

    def _apply_trailing_stop(self, positions, tick):
        """Ajuste le stop loss pour suivre le prix lorsque le trade est en gain."""
        cfg = self._config.get('trailing_stop', {})
        if not cfg.get('enabled', False): return
        
        activation_distance = cfg.get('activation_pips', 250) * 10 * self.point
        trailing_distance = cfg.get('trailing_pips', 200) * 10 * self.point
        
        for pos in positions:
            new_sl = pos.sl
            
            if pos.type == mt5.ORDER_TYPE_BUY:
                if (tick.bid - pos.price_open) < activation_distance: continue
                potential_new_sl = tick.bid - trailing_distance
                if potential_new_sl > pos.sl:
                    new_sl = potential_new_sl
            
            elif pos.type == mt5.ORDER_TYPE_SELL:
                if (pos.price_open - tick.ask) < activation_distance: continue
                potential_new_sl = tick.ask + trailing_distance
                if new_sl == 0 or potential_new_sl < new_sl:
                    new_sl = potential_new_sl

            if new_sl != pos.sl:
                self.log.info(f"TRAILING STOP: MàJ du SL pour #{pos.ticket} de {pos.sl:.5f} à {new_sl:.5f}")
                self._executor.modify_position(pos.ticket, new_sl, pos.tp)