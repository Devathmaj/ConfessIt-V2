import type { CSSProperties } from 'react';
import {
  Bell,
  Camera,
  Crown,
  Dices,
  Flower2,
  Gamepad2,
  Ghost,
  Gift,
  Heart,
  HeartHandshake,
  Mail,
  MessageCircle,
  MessagesSquare,
  Moon,
  Music,
  PartyPopper,
  Quote,
  Send,
  Smile,
  Sparkles,
  Star,
  Trophy,
  User,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';

/**
 * Scattered mid-page ornaments for the gutters and section gaps that the
 * corner florals never reach.
 *
 * Theme fit: outline lucide glyphs only, in the romantic/passion/love-deep
 * tokens at whisper opacity, with a slow `animate-sparkle` twinkle on a few
 * of them. Curated per section — chat glyphs where people talk, game glyphs
 * in the Games shell, keepsakes on romantic pages — so the middle of a page
 * feels dressed without competing with cards or text.
 *
 * Decorative only: the layer is absolute, click-through, hidden from
 * assistive tech, and sits behind content (see `.scatter-icons` in
 * index.css). Gutter icons appear from `md:` up, where centered content
 * actually leaves side space; below that the page is full-bleed content.
 */
export type ScatterVariant = 'romance' | 'chat' | 'games' | 'profile' | 'whimsy';

interface ScatterSpot {
  Icon: LucideIcon;
  /** Absolute placement, e.g. "left-[4%] top-[12%]". */
  pos: string;
  /** Size, e.g. "h-8 w-8". */
  size: string;
  /** Theme color, e.g. "text-romantic". */
  color: string;
  /** Whisper opacity, e.g. "opacity-20". */
  opacity: string;
  /** Gentle twinkle; staggered with `delay`. */
  twinkle?: boolean;
  delay?: string;
}

const ROMANCE: ScatterSpot[] = [
  { Icon: Heart, pos: 'left-[4%] top-[10%]', size: 'h-8 w-8', color: 'text-romantic', opacity: 'opacity-20', twinkle: true, delay: '0.4s' },
  { Icon: Flower2, pos: 'right-[5%] top-[20%]', size: 'h-10 w-10', color: 'text-love-deep', opacity: 'opacity-15' },
  { Icon: Sparkles, pos: 'left-[7%] top-[32%]', size: 'h-5 w-5', color: 'text-passion', opacity: 'opacity-25', twinkle: true, delay: '0.9s' },
  { Icon: Gift, pos: 'right-[4%] top-[44%]', size: 'h-7 w-7', color: 'text-romantic', opacity: 'opacity-15' },
  { Icon: HeartHandshake, pos: 'left-[3%] top-[58%]', size: 'h-9 w-9', color: 'text-love-deep', opacity: 'opacity-15' },
  { Icon: Star, pos: 'right-[7%] top-[70%]', size: 'h-5 w-5', color: 'text-passion', opacity: 'opacity-20', twinkle: true, delay: '1.6s' },
  { Icon: Send, pos: 'left-[6%] top-[84%]', size: 'h-6 w-6', color: 'text-romantic', opacity: 'opacity-15' },
];

const CHAT: ScatterSpot[] = [
  { Icon: MessageCircle, pos: 'left-[4%] top-[12%]', size: 'h-8 w-8', color: 'text-romantic', opacity: 'opacity-20', twinkle: true, delay: '0.3s' },
  { Icon: Mail, pos: 'right-[5%] top-[22%]', size: 'h-7 w-7', color: 'text-love-deep', opacity: 'opacity-15' },
  { Icon: Quote, pos: 'left-[7%] top-[34%]', size: 'h-6 w-6', color: 'text-passion', opacity: 'opacity-20' },
  { Icon: Bell, pos: 'right-[4%] top-[46%]', size: 'h-7 w-7', color: 'text-romantic', opacity: 'opacity-15', twinkle: true, delay: '1.2s' },
  { Icon: MessagesSquare, pos: 'left-[3%] top-[60%]', size: 'h-9 w-9', color: 'text-love-deep', opacity: 'opacity-15' },
  { Icon: Send, pos: 'right-[7%] top-[72%]', size: 'h-5 w-5', color: 'text-passion', opacity: 'opacity-20' },
  { Icon: Heart, pos: 'left-[6%] top-[85%]', size: 'h-6 w-6', color: 'text-romantic', opacity: 'opacity-15', twinkle: true, delay: '1.8s' },
];

const GAMES: ScatterSpot[] = [
  { Icon: Gamepad2, pos: 'left-[4%] top-[12%]', size: 'h-9 w-9', color: 'text-romantic', opacity: 'opacity-20' },
  { Icon: Trophy, pos: 'right-[5%] top-[24%]', size: 'h-8 w-8', color: 'text-love-deep', opacity: 'opacity-15', twinkle: true, delay: '0.5s' },
  { Icon: Dices, pos: 'left-[7%] top-[36%]', size: 'h-7 w-7', color: 'text-passion', opacity: 'opacity-20' },
  { Icon: Star, pos: 'right-[4%] top-[48%]', size: 'h-5 w-5', color: 'text-romantic', opacity: 'opacity-25', twinkle: true, delay: '1.1s' },
  { Icon: Crown, pos: 'left-[3%] top-[60%]', size: 'h-7 w-7', color: 'text-love-deep', opacity: 'opacity-15' },
  { Icon: Zap, pos: 'right-[7%] top-[72%]', size: 'h-6 w-6', color: 'text-passion', opacity: 'opacity-20', twinkle: true, delay: '1.7s' },
  { Icon: PartyPopper, pos: 'left-[6%] top-[85%]', size: 'h-8 w-8', color: 'text-romantic', opacity: 'opacity-15' },
];

const PROFILE: ScatterSpot[] = [
  { Icon: User, pos: 'left-[4%] top-[12%]', size: 'h-8 w-8', color: 'text-romantic', opacity: 'opacity-20' },
  { Icon: Camera, pos: 'right-[5%] top-[24%]', size: 'h-7 w-7', color: 'text-love-deep', opacity: 'opacity-15', twinkle: true, delay: '0.6s' },
  { Icon: Music, pos: 'left-[7%] top-[36%]', size: 'h-6 w-6', color: 'text-passion', opacity: 'opacity-20' },
  { Icon: Heart, pos: 'right-[4%] top-[48%]', size: 'h-7 w-7', color: 'text-romantic', opacity: 'opacity-15' },
  { Icon: Sparkles, pos: 'left-[3%] top-[60%]', size: 'h-5 w-5', color: 'text-love-deep', opacity: 'opacity-20', twinkle: true, delay: '1.3s' },
  { Icon: Star, pos: 'right-[7%] top-[72%]', size: 'h-6 w-6', color: 'text-passion', opacity: 'opacity-20' },
  { Icon: Smile, pos: 'left-[6%] top-[85%]', size: 'h-8 w-8', color: 'text-romantic', opacity: 'opacity-15', twinkle: true, delay: '1.9s' },
];

const WHIMSY: ScatterSpot[] = [
  { Icon: Ghost, pos: 'left-[5%] top-[22%]', size: 'h-10 w-10', color: 'text-romantic', opacity: 'opacity-20' },
  { Icon: Moon, pos: 'right-[6%] top-[30%]', size: 'h-6 w-6', color: 'text-love-deep', opacity: 'opacity-20', twinkle: true, delay: '0.7s' },
  { Icon: Star, pos: 'left-[8%] top-[46%]', size: 'h-5 w-5', color: 'text-passion', opacity: 'opacity-25' },
  { Icon: Sparkles, pos: 'right-[5%] top-[56%]', size: 'h-7 w-7', color: 'text-romantic', opacity: 'opacity-15', twinkle: true, delay: '1.4s' },
  { Icon: Heart, pos: 'left-[5%] top-[70%]', size: 'h-6 w-6', color: 'text-love-deep', opacity: 'opacity-15' },
  { Icon: Music, pos: 'right-[8%] top-[80%]', size: 'h-5 w-5', color: 'text-passion', opacity: 'opacity-20' },
];

const SCATTER_PRESETS: Record<ScatterVariant, ScatterSpot[]> = {
  romance: ROMANCE,
  chat: CHAT,
  games: GAMES,
  profile: PROFILE,
  whimsy: WHIMSY,
};

interface ScatteredIconsProps {
  variant?: ScatterVariant;
  /**
   * Lift above low-z backdrops (e.g. Login's video layer, where a negative
   * layer would hide the art). Content cards still sit above.
   */
  front?: boolean;
  className?: string;
}

export const ScatteredIcons = ({ variant = 'romance', front = false, className }: ScatteredIconsProps) => (
  <div aria-hidden="true" className={cn('scatter-icons', front && 'z-0', className)}>
    {SCATTER_PRESETS[variant].map(({ Icon, pos, size, color, opacity, twinkle, delay }, i) => (
      <Icon
        key={`${variant}-${i}`}
        strokeWidth={1.5}
        className={cn(
          'absolute hidden md:block',
          pos,
          size,
          color,
          opacity,
          twinkle && 'animate-sparkle',
        )}
        style={delay ? ({ animationDelay: delay } as CSSProperties) : undefined}
      />
    ))}
  </div>
);

export default ScatteredIcons;
