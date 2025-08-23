import { useEffect, useState } from 'react';
import { Heart } from 'lucide-react';

interface FloatingHeart {
  id: number;
  left: number;
  size: number;
  delay: number;
}

export const FloatingHearts = () => {
  const [hearts, setHearts] = useState<FloatingHeart[]>([]);

  useEffect(() => {
    const createHeart = () => {
      const heart: FloatingHeart = {
        id: Date.now(),
        left: Math.random() * 100,
        size: Math.random() * 20 + 10,
        delay: Math.random() * 6,
      };
      setHearts(prev => [...prev, heart]);

      // Remove heart after animation completes
      setTimeout(() => {
        setHearts(prev => prev.filter(h => h.id !== heart.id));
      }, 6000 + heart.delay * 1000);
    };

    const interval = setInterval(createHeart, 2000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="floating-hearts">
      {hearts.map(heart => (
        <Heart
          key={heart.id}
          className="heart absolute"
          size={heart.size}
          style={{
            left: `${heart.left}%`,
            animationDelay: `${heart.delay}s`,
            color: Math.random() > 0.5 ? 'hsl(var(--romantic))' : 'hsl(var(--passion))'
          }}
          fill="currentColor"
        />
      ))}
    </div>
  );
};