import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { cn, resolveProfilePictureUrl } from '@/lib/utils';

interface ProfileAvatarProps {
  /** Raw value from the API. Null/empty means the user has set no picture. */
  src?: string | null;
  name?: string | null;
  /** The circle itself: size, border, shadow, spacing. */
  className?: string;
  /** The initial-letter circle: backdrop and letter colour. */
  fallbackClassName?: string;
  alt?: string;
}

/** The first character of the full name, uppercased, for the circle that has no photo. */
const initialLetter = (name?: string | null) => {
  const trimmed = (name ?? '').trim();
  return trimmed ? Array.from(trimmed)[0].toUpperCase() : '?';
};

/**
 * A user's photo, or their first initial when they have no photo.
 *
 * The decision is made on the raw field, not on ``resolveProfilePictureUrl``: that helper
 * answers a missing picture with ``/placeholder.svg``, so testing its result would always
 * be true and the circle would render the generic placeholder tile. A URL that is present
 * but fails to load falls back too -- Radix only mounts the image once it has loaded, so a
 * broken link leaves the initial on screen instead of a blank frame.
 */
export const ProfileAvatar = ({ src, name, className, fallbackClassName, alt }: ProfileAvatarProps) => {
  const resolved = src ? resolveProfilePictureUrl(src) : null;
  const picture = resolved && resolved !== '/placeholder.svg' ? resolved : null;

  return (
    <Avatar className={cn('shrink-0', className)}>
      {picture && <AvatarImage src={picture} alt={alt ?? name ?? ''} className="object-cover" />}
      <AvatarFallback
        role="img"
        aria-label={name ?? alt ?? 'Profile picture'}
        className={cn('avatar', fallbackClassName)}
      >
        {/* Rendered as SVG so the letter scales with whatever circle size the caller
            passes, instead of needing a matching text size at every call site. */}
        <svg viewBox="0 0 100 100" className="h-full w-full" aria-hidden="true" focusable="false">
          <text
            x="50"
            y="50"
            dy="0.35em"
            textAnchor="middle"
            fontSize="52"
            fontWeight="600"
            fill="currentColor"
          >
            {initialLetter(name)}
          </text>
        </svg>
      </AvatarFallback>
    </Avatar>
  );
};
