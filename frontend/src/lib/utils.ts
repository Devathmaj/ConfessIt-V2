import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function resolveProfilePictureUrl(value?: string | null) {
  if (!value) {
    return '/placeholder.svg'
  }

  if (/^https?:\/\//i.test(value)) {
    return value
  }

  if (value.startsWith('data:')) {
    return value
  }

  if (value.startsWith('/')) {
    return value
  }

  return '/placeholder.svg'
}
