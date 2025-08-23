import type { Config } from "tailwindcss";

export default {
	darkMode: ["class"],
	content: [
		"./pages/**/*.{ts,tsx}",
		"./components/**/*.{ts,tsx}",
		"./app/**/*.{ts,tsx}",
		"./src/**/*.{ts,tsx}",
	],
	prefix: "",
	theme: {
		container: {
			center: true,
			padding: '2rem',
			screens: {
				'2xl': '1400px'
			}
		},
		extend: {
			fontFamily: {
				montserrat: ['Montserrat', 'sans-serif'],
			},
			colors: {
				border: 'hsl(var(--border))',
				input: 'hsl(var(--input))',
				ring: 'hsl(var(--ring))',
				background: 'hsl(var(--background))',
				foreground: 'hsl(var(--foreground))',
				primary: {
					DEFAULT: 'hsl(var(--primary))',
					foreground: 'hsl(var(--primary-foreground))',
					glow: 'hsl(var(--primary-glow))'
				},
				secondary: {
					DEFAULT: 'hsl(var(--secondary))',
					foreground: 'hsl(var(--secondary-foreground))'
				},
				romantic: {
					DEFAULT: 'hsl(var(--romantic))',
					light: 'hsl(var(--romantic-light))',
					dark: 'hsl(var(--romantic-dark))'
				},
				passion: {
					DEFAULT: 'hsl(var(--passion))',
					light: 'hsl(var(--passion-light))'
				},
				love: {
					DEFAULT: 'hsl(var(--love))',
					deep: 'hsl(var(--love-deep))'
				},
				destructive: {
					DEFAULT: 'hsl(var(--destructive))',
					foreground: 'hsl(var(--destructive-foreground))'
				},
				muted: {
					DEFAULT: 'hsl(var(--muted))',
					foreground: 'hsl(var(--muted-foreground))'
				},
				accent: {
					DEFAULT: 'hsl(var(--accent))',
					foreground: 'hsl(var(--accent-foreground))'
				},
				popover: {
					DEFAULT: 'hsl(var(--popover))',
					foreground: 'hsl(var(--popover-foreground))'
				},
				card: {
					DEFAULT: 'hsl(var(--card))',
					foreground: 'hsl(var(--card-foreground))'
				}
			},
			backgroundImage: {
				'gradient-romantic': 'var(--gradient-romantic)',
				'gradient-love': 'var(--gradient-love)',
				'gradient-passion': 'var(--gradient-passion)',
				'gradient-hero': 'var(--gradient-hero)',
				'gradient-card': 'var(--gradient-card)',
			},
			boxShadow: {
				'romantic': 'var(--shadow-romantic)',
				'love': 'var(--shadow-love)',
				'glow': 'var(--shadow-glow)',
			},
			borderRadius: {
				lg: 'var(--radius)',
				md: 'calc(var(--radius) - 2px)',
				sm: 'calc(var(--radius) - 4px)'
			},
			keyframes: {
				'accordion-down': {
					from: { height: '0' },
					to: { height: 'var(--radix-accordion-content-height)' }
				},
				'accordion-up': {
					from: { height: 'var(--radix-accordion-content-height)' },
					to: { height: '0' }
				},
				'float-up': {
					'0%': { opacity: '0', transform: 'translateY(100vh) scale(0)' },
					'10%': { opacity: '1', transform: 'translateY(90vh) scale(1)' },
					'90%': { opacity: '1', transform: 'translateY(10vh) scale(1)' },
					'100%': { opacity: '0', transform: 'translateY(0) scale(0)' }
				},
				'pulse-heart': {
					'0%': { transform: 'scale(1)' },
					'50%': { transform: 'scale(1.1)' },
					'100%': { transform: 'scale(1)' }
				},
				'sparkle': {
					'0%, 100%': { opacity: '0', transform: 'scale(0.8)' },
					'50%': { opacity: '1', transform: 'scale(1.2)' }
				},
				'slot-spin': {
					'0%': { transform: 'translateY(0)' },
					'100%': { transform: 'translateY(-100%)' }
				},
				'fade-in': {
					'0%': { opacity: '0', transform: 'translateY(20px)' },
					'100%': { opacity: '1', transform: 'translateY(0)' }
				}
			},
			animation: {
				'accordion-down': 'accordion-down 0.2s ease-out',
				'accordion-up': 'accordion-up 0.2s ease-out',
				'float-up': 'float-up 6s infinite linear',
				'pulse-heart': 'pulse-heart 1.5s infinite',
				'sparkle': 'sparkle 2s infinite',
				'slot-spin': 'slot-spin 0.1s infinite linear',
				'fade-in': 'fade-in 0.6s ease-out'
			},
			transitionTimingFunction: {
				'smooth': 'cubic-bezier(0.23, 1, 0.320, 1)',
				'bounce': 'cubic-bezier(0.68, -0.55, 0.265, 1.55)'
			}
		}
	},
	plugins: [require("tailwindcss-animate")],
} satisfies Config;
