import random

class JokerGame:
    def __init__(self):
        self.players = {}       # {sid: {name: "Alex", hand: [], score: 0}}
        self.bids = {}          # {sid: 3}
        self.tricks_won = {}    # {sid: 1}
        self.ready_players = set()
        
        self.deck = []
        self.cards_to_deal = 1
        self.trump_card = None
        self.trump_suit = None
        
        self.game_phase = "WAITING" 
        self.turn_order = []    # List of sids in order
        self.round_number = 1   
        self.dealer_index = -1  # Index in turn_order (0-3)
        self.current_bidder_index = 0

    def add_player(self, sid, name):
        if len(self.players) < 4:
            self.players[sid] = {"name": name, "hand": [], "score": 0}
            self.turn_order.append(sid)
            return True
        return False

    def mark_ready(self, sid):
        self.ready_players.add(sid)
        # Check if ALL 4 are present AND ready
        if len(self.players) == 4 and len(self.ready_players) == 4:
            return True
        return False

    def create_deck(self):
        self.deck = []
        suits = ['H', 'D', 'C', 'S']
        ranks = ['7', '8', '9', '10', 'J', 'Q', 'K', 'A']
        
        # 1. 7 through Ace
        for s in suits:
            for r in ranks:
                self.deck.append({"rank": r, "suit": s, "value": f"{r}{s}"})
        
        # 2. Red 6s
        self.deck.append({"rank": "6", "suit": "H", "value": "6H"})
        self.deck.append({"rank": "6", "suit": "D", "value": "6D"})
        
        # 3. Jokers
        self.deck.append({"rank": "Joker", "suit": "Red", "value": "JKR"})
        self.deck.append({"rank": "Joker", "suit": "Black", "value": "JKB"})
        
        random.shuffle(self.deck)

    def perform_ace_hunt(self):
        """
        Deals cards until an Ace is found to determine the first Dealer.
        """
        self.create_deck()
        ace_hunt_log = []
        found_ace = False
        current_idx = 0
        
        # Loop until Ace
        while not found_ace and self.deck:
            card = self.deck.pop()
            sid = self.turn_order[current_idx]
            name = self.players[sid]['name']
            is_ace = (card['rank'] == 'A')
            
            ace_hunt_log.append({
                'sid': sid, 'name': name, 'card': card, 'is_ace': is_ace
            })
            
            if is_ace:
                found_ace = True
                self.dealer_index = current_idx # Winner becomes Dealer
            else:
                current_idx = (current_idx + 1) % 4
                
        return ace_hunt_log

    def start_new_round(self):
        # 1. Rotate Dealer (If not Round 1)
        if self.round_number > 1:
            self.dealer_index = (self.dealer_index + 1) % 4
            
        # 2. Determine who bids first (Left of Dealer)
        self.current_bidder_index = (self.dealer_index + 1) % 4
        
        # 3. Setup Round
        self.create_deck()
        self.bids = {}
        self.tricks_won = {sid: 0 for sid in self.players}
        self.game_phase = "BIDDING"
        
        # 4. Deal Cards (Logic for 1-9-1 ladder can be added here later)
        # For now, cards = round number
        self.cards_to_deal = self.round_number 
        
        for sid in self.players:
            hand = []
            for _ in range(self.cards_to_deal):
                if self.deck:
                    hand.append(self.deck.pop())
            # Sort: Jokers first, then Suits
            hand.sort(key=lambda x: (x['rank'] == 'Joker', x['suit'], x['rank']))
            self.players[sid]["hand"] = hand
            
        # 5. Set Trump / Kozer
        if self.deck:
            self.trump_card = self.deck.pop()
            self.trump_suit = self.trump_card['suit']
            if self.trump_card['rank'] == 'Joker':
                self.trump_suit = "NT" # No Trump
                self.trump_card['value'] = "NO TRUMP (Joker)"
        else:
            self.trump_card = {"rank": "No", "suit": "Trump", "value": "NT"}
            self.trump_suit = "NT"
            
        return self.get_current_bidder_id()

    def get_current_bidder_id(self):
        if not self.turn_order: return None
        return self.turn_order[self.current_bidder_index]

    def get_forbidden_bid(self, player_sid):
        # Only restriction for the LAST bidder (The Dealer)
        if len(self.bids) < 3:
            return None 

        current_sum = sum(self.bids.values())
        forbidden = self.cards_to_deal - current_sum
        return forbidden if forbidden >= 0 else None

    def process_bid(self, player_sid, amount):
        if player_sid != self.get_current_bidder_id():
            return False, "Not your turn!"

        forbidden = self.get_forbidden_bid(player_sid)
        if forbidden is not None and amount == forbidden:
            return False, "Forbidden Bid! (Sum rule)"

        self.bids[player_sid] = amount
        
        # Move to next player
        self.current_bidder_index = (self.current_bidder_index + 1) % 4
        
        # Check if Bidding is over
        is_bidding_over = (len(self.bids) == 4)
        if is_bidding_over:
            self.game_phase = "PLAYING"
            # Reset turn to First Bidder (Left of Dealer) for playing cards
            self.current_bidder_index = (self.dealer_index + 1) % 4
            
        return True, is_bidding_over
    
    # Add these inside JokerGame class

    def play_card(self, sid, card_index):
        # 1. Validate Turn
        if sid != self.get_current_bidder_id():
            return False, "Not your turn!"
            
        # 2. Get the card
        hand = self.players[sid]['hand']
        if card_index >= len(hand):
            return False, "Invalid card!"
            
        card = hand[card_index]
        
        # 3. Validate Rules (Must follow suit)
        # We need to know what cards are already on the table
        table_cards = getattr(self, 'current_trick_cards', [])
        
        # Simple Validation (We can add Strict Joker Rules here later)
        # For now, let's allow the move so you can test the UI
        
        # 4. Move Card: Hand -> Table
        played_card = hand.pop(card_index)
        
        if not hasattr(self, 'current_trick_cards'):
            self.current_trick_cards = []
            
        self.current_trick_cards.append({
            'sid': sid,
            'card': played_card,
            'name': self.players[sid]['name']
        })
        
        # 5. Move Turn to Next Player
        self.current_bidder_index = (self.current_bidder_index + 1) % 4
        
        return True, played_card

    def check_trick_end(self):
        """ Returns the Winner SID if 4 cards played, else None """
        if not hasattr(self, 'current_trick_cards'): return None
        
        if len(self.current_trick_cards) == 4:
            # 1. Calculate Winner (Simplified High Card Logic for testing)
            # In real Joker, we check Trump/Kozer here
            winner = self.resolve_winner(self.current_trick_cards)
            
            # 2. Give point/trick to winner
            self.tricks_won[winner['sid']] += 1
            
            # 3. Set Winner as next leader
            self.current_bidder_index = self.turn_order.index(winner['sid'])
            
            # 4. Clear Table (in memory, UI handles visual clear)
            self.current_trick_cards = []
            
            return winner
            
        return None

    def resolve_winner(self, trick):
        # Logic: 
        # 1. Check for Jokers
        # 2. Check for Trump
        # 3. Check for Leading Suit (First card played)
        
        lead_suit = trick[0]['card']['suit']
        trump_suit = self.trump_suit
        
        best_card = trick[0]
        
        for i in range(1, 4):
            challenger = trick[i]
            c_card = challenger['card']
            b_card = best_card['card']
            
            # Joker Logic (Assuming Joker beats all for now)
            if c_card['rank'] == 'Joker' and b_card['rank'] != 'Joker':
                best_card = challenger
                continue
                
            # Trump Logic
            if c_card['suit'] == trump_suit and b_card['suit'] != trump_suit:
                best_card = challenger
                continue
                
            # Follow Suit Logic (Higher rank of same suit)
            if c_card['suit'] == lead_suit and b_card['suit'] == lead_suit:
                # Compare Ranks
                ranks = ['6','7','8','9','10','J','Q','K','A']
                if ranks.index(c_card['rank']) > ranks.index(b_card['rank']):
                    best_card = challenger
                    
        return best_card