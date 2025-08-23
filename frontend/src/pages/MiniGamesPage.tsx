import { useState } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { CountdownTimer } from '@/components/ui/countdown-timer';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { 
  Gamepad2, 
  Heart, 
  Sparkles,
  Send,
  Shuffle,
  Star,
  Gift,
  MessageCircle
} from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';

interface IcebreakerCard {
  id: string;
  question: string;
  category: 'fun' | 'deep' | 'romantic' | 'silly';
  color: string;
}

interface MiniGame {
  id: string;
  name: string;
  description: string;
  icon: React.ComponentType<any>;
  color: string;
  difficulty: 'easy' | 'medium' | 'hard';
}

const icebreakerCards: IcebreakerCard[] = [
  {
    id: '1',
    question: "If you could have dinner with anyone, dead or alive, who would it be and why?",
    category: 'deep',
    color: 'bg-gradient-romantic'
  },
  {
    id: '2',
    question: "What's your most embarrassing moment that you can laugh about now?",
    category: 'fun',
    color: 'bg-gradient-love'
  },
  {
    id: '3',
    question: "If you could travel anywhere in the world right now, where would you go?",
    category: 'fun',
    color: 'bg-gradient-passion'
  },
  {
    id: '4',
    question: "What's the cheesiest pickup line you know?",
    category: 'silly',
    color: 'bg-gradient-hero'
  },
  {
    id: '5',
    question: "What's your idea of a perfect first date?",
    category: 'romantic',
    color: 'bg-gradient-romantic'
  },
  {
    id: '6',
    question: "If you could have any superpower, what would it be?",
    category: 'fun',
    color: 'bg-gradient-love'
  },
  {
    id: '7',
    question: "What's the best compliment you've ever received?",
    category: 'deep',
    color: 'bg-gradient-passion'
  },
  {
    id: '8',
    question: "What song makes you instantly happy?",
    category: 'fun',
    color: 'bg-gradient-hero'
  }
];

const miniGames: MiniGame[] = [
  {
    id: 'truth-dare',
    name: 'Truth or Dare',
    description: 'Classic game with romantic twists',
    icon: Heart,
    color: 'bg-gradient-romantic',
    difficulty: 'easy'
  },
  {
    id: 'love-quiz',
    name: 'Love Compatibility Quiz',
    description: 'Test your romantic compatibility',
    icon: Star,
    color: 'bg-gradient-love',
    difficulty: 'medium'
  },
  {
    id: 'word-game',
    name: 'Love Letter Builder',
    description: 'Build romantic sentences together',
    icon: MessageCircle,
    color: 'bg-gradient-passion',
    difficulty: 'easy'
  },
  {
    id: 'memory-match',
    name: 'Hearts Memory Match',
    description: 'Match romantic symbols and quotes',
    icon: Gift,
    color: 'bg-gradient-hero',
    difficulty: 'hard'
  }
];

const mockMatches = [
  { name: 'Alex Chen', avatar: '🌟' },
  { name: 'Jordan Smith', avatar: '🎨' },
  { name: 'Sam Rivera', avatar: '🎵' },
  { name: 'Casey Park', avatar: '🌺' }
];

export const MiniGamesPage = () => {
  const { user } = useAuth();
  const [flippedCards, setFlippedCards] = useState<string[]>([]);
  const [selectedGame, setSelectedGame] = useState<MiniGame | null>(null);
  const [gameStarted, setGameStarted] = useState(false);

  const flipCard = (cardId: string) => {
    setFlippedCards(prev => 
      prev.includes(cardId) 
        ? prev.filter(id => id !== cardId)
        : [...prev, cardId]
    );
  };

  const sendIcebreaker = (card: IcebreakerCard) => {
    const randomMatch = mockMatches[Math.floor(Math.random() * mockMatches.length)];
    toast.success(`Icebreaker sent to ${randomMatch.name}! 💕`);
  };

  const startGame = (game: MiniGame) => {
    setSelectedGame(game);
    setGameStarted(true);
    toast.success(`Starting ${game.name}! 🎮`);
  };

  return (
    <div className="min-h-screen p-4 pt-24 relative overflow-hidden">
      <Navigation />
      <FloatingHearts />
      
      {/* Header */}
      <div className="max-w-7xl mx-auto mb-8">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-6">
          <div className="text-center lg:text-left">
            <h1 className="text-5xl font-dancing text-romantic mb-2">
              Mini-Games & Icebreakers 🎮
            </h1>
            <p className="text-xl text-muted-foreground">
              Break the ice and have fun together!
            </p>
          </div>
          <CountdownTimer />
        </div>
      </div>

      {!gameStarted ? (
        <div className="max-w-7xl mx-auto space-y-12">
          {/* Icebreaker Cards Section */}
          <section>
            <div className="text-center mb-8">
              <h2 className="text-4xl font-dancing text-romantic mb-4">Icebreaker Cards</h2>
              <p className="text-lg text-muted-foreground">Click to flip and send to your matches!</p>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
              {icebreakerCards.map((card) => (
                <div
                  key={card.id}
                  className="relative h-64 perspective-1000 cursor-pointer"
                  onClick={() => flipCard(card.id)}
                >
                  <div className={`card-flip ${flippedCards.includes(card.id) ? 'flipped' : ''}`}>
                    {/* Front of card */}
                    <div className="card-face card-front">
                      <Card className="h-full confession-card">
                        <CardContent className="h-full flex flex-col items-center justify-center p-6 relative overflow-hidden">
                          <div className={`absolute inset-0 ${card.color} opacity-20`} />
                          <div className="relative z-10 text-center">
                            <Sparkles className="w-12 h-12 text-romantic mx-auto mb-4 animate-pulse" />
                            <h3 className="font-dancing text-2xl text-romantic mb-2">Icebreaker</h3>
                            <p className="text-sm text-muted-foreground capitalize">{card.category}</p>
                            <div className="absolute -top-2 -right-2 w-6 h-6 bg-romantic rounded-full flex items-center justify-center">
                              <span className="text-white text-xs">?</span>
                            </div>
                          </div>
                        </CardContent>
                      </Card>
                    </div>

                    {/* Back of card */}
                    <div className="card-face card-back">
                      <Card className="h-full confession-card">
                        <CardContent className="h-full flex flex-col justify-between p-4 relative overflow-hidden">
                          <div className={`absolute inset-0 ${card.color} opacity-10`} />
                          <div className="relative z-10">
                            <p className="text-sm text-center mb-4 text-romantic-dark font-medium leading-relaxed">
                              {card.question}
                            </p>
                          </div>
                          <Button
                            onClick={(e) => {
                              e.stopPropagation();
                              sendIcebreaker(card);
                            }}
                            size="sm"
                            className="w-full bg-gradient-romantic hover:opacity-90 text-white font-dancing"
                          >
                            <Send className="w-4 h-4 mr-2" />
                            Send
                          </Button>
                        </CardContent>
                      </Card>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </section>

          {/* Mini Games Section */}
          <section>
            <div className="text-center mb-8">
              <h2 className="text-4xl font-dancing text-romantic mb-4">Mini-Games</h2>
              <p className="text-lg text-muted-foreground">Fun games to play with your matches!</p>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
              {miniGames.map((game) => {
                const IconComponent = game.icon;
                return (
                  <Card 
                    key={game.id}
                    className="confession-card cursor-pointer group hover:scale-105 transition-all duration-300"
                    onClick={() => startGame(game)}
                  >
                    <CardHeader>
                      <div className={`w-16 h-16 ${game.color} rounded-lg flex items-center justify-center mb-3 group-hover:scale-110 transition-all duration-300 mx-auto`}>
                        <IconComponent className="w-8 h-8 text-white" />
                      </div>
                      <CardTitle className="text-xl font-dancing text-romantic text-center">
                        {game.name}
                      </CardTitle>
                      <CardDescription className="text-center">
                        {game.description}
                      </CardDescription>
                    </CardHeader>
                    <CardContent>
                      <div className="text-center space-y-3">
                        <div className="flex justify-center">
                          <span className={`px-3 py-1 rounded-full text-xs font-semibold ${
                            game.difficulty === 'easy' 
                              ? 'bg-green-100 text-green-800' 
                              : game.difficulty === 'medium'
                              ? 'bg-yellow-100 text-yellow-800'
                              : 'bg-red-100 text-red-800'
                          }`}>
                            {game.difficulty.charAt(0).toUpperCase() + game.difficulty.slice(1)}
                          </span>
                        </div>
                        <Button 
                          className="w-full bg-gradient-romantic hover:opacity-90 text-white font-dancing"
                          size="sm"
                        >
                          <Gamepad2 className="w-4 h-4 mr-2" />
                          Play Now
                        </Button>
                      </div>
                    </CardContent>
                  </Card>
                );
              })}
            </div>
          </section>

          {/* Quick Match Section */}
          <section>
            <Card className="confession-card">
              <CardHeader>
                <CardTitle className="text-3xl font-dancing text-romantic text-center">
                  Quick Match for Games
                </CardTitle>
                <CardDescription className="text-center">
                  Find someone to play games with right now!
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="flex flex-col items-center space-y-6">
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                    {mockMatches.map((match, index) => (
                      <div key={index} className="text-center group cursor-pointer">
                        <div className="w-16 h-16 bg-gradient-love rounded-full flex items-center justify-center mb-2 group-hover:scale-110 transition-all duration-300">
                          <span className="text-2xl">{match.avatar}</span>
                        </div>
                        <p className="text-sm font-dancing text-romantic">{match.name}</p>
                        <p className="text-xs text-muted-foreground">Online</p>
                      </div>
                    ))}
                  </div>
                  
                  <Button 
                    className="bg-gradient-passion hover:opacity-90 text-white font-dancing px-8 py-3"
                    onClick={() => {
                      const randomMatch = mockMatches[Math.floor(Math.random() * mockMatches.length)];
                      toast.success(`Inviting ${randomMatch.name} to play! 🎮`);
                    }}
                  >
                    <Shuffle className="w-5 h-5 mr-2" />
                    Invite Random Player
                  </Button>
                </div>
              </CardContent>
            </Card>
          </section>
        </div>
      ) : (
        /* Game Interface */
        <div className="max-w-4xl mx-auto">
          <Card className="confession-card">
            <CardHeader>
              <div className="flex items-center gap-4">
                <Button 
                  variant="ghost" 
                  onClick={() => {
                    setGameStarted(false);
                    setSelectedGame(null);
                  }}
                >
                  ← Back
                </Button>
                <CardTitle className="text-3xl font-dancing text-romantic">
                  {selectedGame?.name}
                </CardTitle>
              </div>
            </CardHeader>
            <CardContent>
              <div className="text-center py-20">
                <div className={`w-24 h-24 ${selectedGame?.color} rounded-full flex items-center justify-center mx-auto mb-8 animate-pulse`}>
                  {selectedGame?.icon && <selectedGame.icon className="w-12 h-12 text-white" />}
                </div>
                <h3 className="text-2xl font-dancing text-romantic mb-4">Game Starting Soon!</h3>
                <p className="text-muted-foreground mb-8">
                  {selectedGame?.description}
                </p>
                <div className="space-y-4">
                  <p className="text-lg font-dancing text-romantic">
                    Waiting for your match to join...
                  </p>
                  <div className="flex justify-center space-x-2">
                    {[...Array(3)].map((_, i) => (
                      <div 
                        key={i}
                        className="w-3 h-3 bg-romantic rounded-full animate-bounce"
                        style={{ animationDelay: `${i * 0.2}s` }}
                      />
                    ))}
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

    </div>
  );
};