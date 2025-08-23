import { useEffect, useState } from 'react';
import { Heart } from 'lucide-react';

interface TimeLeft {
  hours: number;
  minutes: number;
  seconds: number;
}

export const CountdownTimer = () => {
  const [timeLeft, setTimeLeft] = useState<TimeLeft>({ hours: 0, minutes: 0, seconds: 0 });

  useEffect(() => {
    // Valentine's Day event ends at midnight
    const eventEnd = new Date();
    eventEnd.setHours(23, 59, 59, 999);

    const timer = setInterval(() => {
      const now = new Date().getTime();
      const distance = eventEnd.getTime() - now;

      const hours = Math.floor(distance / (1000 * 60 * 60));
      const minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
      const seconds = Math.floor((distance % (1000 * 60)) / 1000);

      if (distance < 0) {
        setTimeLeft({ hours: 0, minutes: 0, seconds: 0 });
      } else {
        setTimeLeft({ hours, minutes, seconds });
      }
    }, 1000);

    return () => clearInterval(timer);
  }, []);

  return (
    <div className="bg-gradient-romantic text-white px-6 py-3 rounded-full shadow-romantic flex items-center gap-3">
      <Heart className="animate-pulse-heart" size={20} fill="currentColor" />
      <div className="font-dancing text-lg font-semibold">
        Event ends in: {String(timeLeft.hours).padStart(2, '0')}:
        {String(timeLeft.minutes).padStart(2, '0')}:
        {String(timeLeft.seconds).padStart(2, '0')}
      </div>
      <Heart className="animate-pulse-heart" size={20} fill="currentColor" />
    </div>
  );
};