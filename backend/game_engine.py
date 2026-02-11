import random

class JokerGame:
    def __init__(self):
        self.players = {}       
        self.bids = {}          
        self.tricks_won = {}    
        self.ready_players = set()
        
        self.deck = []
        self.cards_to_deal = 1
        self.trump_card = None
        self.trump_suit = None
        
        self.game_phase = "WAITING" 
        self.turn_order = []    
        self.round_number = 1   
        self.dealer_index = -1  
        self.current_bidder_index = 0
        self.current_trick_cards = []
        self.lead_override_suit = None 

    def add_player(self, sid, name):
        if len(self.players) < 4:
            self.players[sid] = {"name": name, "hand": [], "score": 0}
            self.turn_order.append(sid)
            return True
        return False

    def mark_ready(self, sid):
        self.ready_players.add(sid)
        if len(self.players) == 4 and len(self.ready_players) == 4:
            return True
        return False

    def create_deck(self):
        self.deck = []
        suits = ['H', 'D', 'C', 'S']
        ranks = ['7', '8', '9', '10', 'J', 'Q', 'K', 'A']
        for s in suits:
            for r in ranks:
                self.deck.append({"rank": r, "suit": s, "value": f"{r}{s}"})
        self.deck.append({"rank": "6", "suit": "H", "value": "6H"})
        self.deck.append({"rank": "6", "suit": "D", "value": "6D"})
        self.deck.append({"rank": "Joker", "suit": "Red", "value": "JKR"})
        self.deck.append({"rank": "Joker", "suit": "Black", "value": "JKB"})
        random.shuffle(self.deck)

    def perform_ace_hunt(self):
        self.create_deck()
        ace_hunt_log = []
        found_ace = False
        current_idx = 0
        while not found_ace and self.deck:
            card = self.deck.pop()
            if not self.turn_order: break 
            sid = self.turn_order[current_idx]
            name = self.players[sid]['name']
            is_ace = (card['rank'] == 'A')
            ace_hunt_log.append({'sid': sid, 'name': name, 'card': card, 'is_ace': is_ace})
            if is_ace:
                found_ace = True
                self.dealer_index = current_idx
            else:
                current_idx = (current_idx + 1) % 4
        return ace_hunt_log

    def start_new_round(self):
        if self.round_number > 1:
            self.dealer_index = (self.dealer_index + 1) % 4
        self.current_bidder_index = (self.dealer_index + 1) % 4
        self.create_deck()
        self.bids = {}
        self.tricks_won = {sid: 0 for sid in self.players}
        self.game_phase = "BIDDING"
        self.current_trick_cards = []
        self.tricks_played_in_round = 0 
        self.cards_to_deal = self.round_number 
        self.lead_override_suit = None
        
        for sid in self.players:
            hand = []
            for _ in range(self.cards_to_deal):
                if self.deck: hand.append(self.deck.pop())
            hand.sort(key=lambda x: (x['rank'] == 'Joker', x['suit'], x['rank']))
            self.players[sid]["hand"] = hand
            
        if self.deck:
            self.trump_card = self.deck.pop()
            self.trump_suit = self.trump_card['suit']
            if self.trump_card['rank'] == 'Joker':
                self.trump_suit = "NT" 
                self.trump_card['value'] = "NO TRUMP (Joker)"
        else:
            self.trump_card = {"rank": "No", "suit": "Trump", "value": "NT"}
            self.trump_suit = "NT"
        return self.get_current_bidder_id()

    def get_current_bidder_id(self):
        if not self.turn_order: return None
        if self.current_bidder_index >= len(self.turn_order): self.current_bidder_index = 0
        return self.turn_order[self.current_bidder_index]

    def get_forbidden_bid(self, player_sid):
        if len(self.bids) < 3: return None 
        current_sum = sum(self.bids.values())
        forbidden = self.cards_to_deal - current_sum
        return forbidden if forbidden >= 0 else None

    def process_bid(self, player_sid, amount):
        if player_sid != self.get_current_bidder_id():
            return False, "Not your turn!"
        forbidden = self.get_forbidden_bid(player_sid)
        if forbidden is not None and amount == forbidden:
            return False, "Forbidden Bid!"
        self.bids[player_sid] = amount
        self.current_bidder_index = (self.current_bidder_index + 1) % 4
        is_bidding_over = (len(self.bids) == 4)
        if is_bidding_over:
            self.game_phase = "PLAYING"
            self.current_bidder_index = (self.dealer_index + 1) % 4
        return True, is_bidding_over

    def is_move_valid(self, sid, card_to_play):
        if card_to_play['rank'] == 'Joker': return True, ""
        if not self.current_trick_cards: return True, ""

        if self.lead_override_suit: lead_suit = self.lead_override_suit
        else: lead_suit = self.current_trick_cards[0]['card']['suit']

        played_suit = card_to_play['suit']
        hand = self.players[sid]['hand']
        
        has_lead_suit = any(c['suit'] == lead_suit and c['rank'] != 'Joker' for c in hand)
        has_trump = False
        if self.trump_suit != "NT":
            has_trump = any(c['suit'] == self.trump_suit and c['rank'] != 'Joker' for c in hand)

        if has_lead_suit:
            if played_suit == lead_suit: return True, ""
            return False, f"You must play {lead_suit}!"

        if has_trump:
            if played_suit == self.trump_suit: return True, ""
            return False, f"You must play Kozer ({self.trump_suit})!"

        return True, ""

    def play_card(self, sid, card_index, joker_data=None):
        if sid != self.get_current_bidder_id(): return False, "Not your turn!"
            
        hand = self.players[sid]['hand']
        if card_index >= len(hand): return False, "Invalid card"
        
        card_to_play = hand[card_index]
        valid, msg = self.is_move_valid(sid, card_to_play)
        if not valid: return False, msg
        
        played_card = hand.pop(card_index)
        
        if played_card['rank'] == 'Joker' and joker_data:
            action = joker_data.get('joker_action')
            suit_req = joker_data.get('joker_suit')
            if suit_req == 'TRUMP':
                suit_req = self.trump_suit if self.trump_suit != 'NT' else 'H' 
            
            played_card['virtual_action'] = action
            played_card['virtual_suit'] = suit_req
            
            if not self.current_trick_cards:
                self.lead_override_suit = suit_req
                played_card['rank_value'] = 999 if action == 'TAKE' else 0
            else:
                if action == 'TAKE':
                    played_card['virtual_suit'] = self.trump_suit 
                    played_card['rank_value'] = 1000 
                else:
                    played_card['virtual_suit'] = self.current_trick_cards[0]['card']['suit']
                    played_card['rank_value'] = -1
        else:
            played_card['rank_value'] = self.get_rank_value(played_card['rank'])
            played_card['virtual_suit'] = played_card['suit']

        self.current_trick_cards.append({
            'sid': sid, 'card': played_card, 'name': self.players[sid]['name']
        })
        self.current_bidder_index = (self.current_bidder_index + 1) % 4
        return True, played_card

    def check_trick_end(self):
        if len(self.current_trick_cards) == 4:
            winner = self.resolve_winner(self.current_trick_cards)
            self.tricks_won[winner['sid']] += 1
            if not hasattr(self, 'tricks_played_in_round'): self.tricks_played_in_round = 0
            self.tricks_played_in_round += 1
            
            self.current_trick_cards = [] 
            self.lead_override_suit = None 
            
            is_round_over = (self.tricks_played_in_round == self.cards_to_deal)
            self.current_bidder_index = self.turn_order.index(winner['sid'])
            
            return {'winner': winner, 'round_over': is_round_over}
        return None

    # --- NEW: SCORE CALCULATION LOGIC ---
    def calculate_round_scores(self):
        round_log = {}
        for sid in self.players:
            bid = self.bids.get(sid, 0)
            won = self.tricks_won.get(sid, 0)
            round_score = 0
            
            # 1. GRAND SLAM (ALL CARDS)
            if bid == self.cards_to_deal:
                if won == bid:
                    # Success: 4/4 -> 400
                    round_score = bid * 100
                else:
                    # Fail: 3/4 -> -400
                    round_score = -(bid * 100)
            
            # 2. UNDER BID (FAIL)
            elif won < bid:
                # 2/3 -> -200
                round_score = -((bid + 1) * 50)
                
            # 3. SUCCESS (EXACT)
            elif won == bid:
                # 3/3 -> 200
                round_score = (bid + 1) * 50
                
            # 4. OVER BID (MORE)
            elif won > bid:
                # 4/2 -> 40
                round_score = won * 10
            
            # Update Total Score
            self.players[sid]['score'] += round_score
            round_log[sid] = round_score
            
        return round_log

    def resolve_winner(self, trick):
        first_card = trick[0]['card']
        if first_card['rank'] == 'Joker':
            lead_suit = first_card['virtual_suit']
        else:
            lead_suit = first_card['suit']
            
        trump_suit = self.trump_suit
        best_play = trick[0]
        
        for i in range(1, 4):
            challenger = trick[i]
            c_card = challenger['card']
            b_card = best_play['card']
            
            if c_card['rank'] == 'Joker' and b_card['rank'] == 'Joker':
                if c_card['virtual_action'] == 'TAKE': best_play = challenger
                continue

            c_suit = c_card.get('virtual_suit', c_card['suit'])
            b_suit = b_card.get('virtual_suit', b_card['suit'])
            c_is_trump = (c_suit == trump_suit)
            b_is_trump = (b_suit == trump_suit)
            
            if c_is_trump and not b_is_trump: best_play = challenger
            elif c_is_trump and b_is_trump:
                if c_card['rank_value'] > b_card['rank_value']: best_play = challenger
            elif not b_is_trump and c_suit == lead_suit:
                if b_suit != lead_suit: best_play = challenger
                elif c_card['rank_value'] > b_card['rank_value']: best_play = challenger
        return best_play

    def get_rank_value(self, rank):
        order = ['6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
        if rank == 'Joker': return 99 
        if rank in order: return order.index(rank) + 1 
        return 0