import { Link, useLocation } from "react-router-dom";
import { useEffect } from "react";
import { FloralDecor } from "@/components/FloralDecor";
import { ScatteredIcons } from "@/components/ScatteredIcons";
import { FloatingHearts } from "@/components/ui/floating-hearts";

const NotFound = () => {
  const location = useLocation();

  useEffect(() => {
    console.error(
      "404 Error: User attempted to access non-existent route:",
      location.pathname
    );
  }, [location.pathname]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-100 relative overflow-hidden">
      <FloatingHearts />
      {/* Floral edges: 4126474 top-left; 1706715_top top-right;
          1748704 bottom-left (+ vertical-axis mirror bottom-right). */}
      <FloralDecor pair={3} className="left-0 top-0 w-[34rem] opacity-90 sm:w-[42rem]" />
      <FloralDecor pair={6} className="right-0 top-0 w-[34rem] opacity-90 sm:w-[42rem]" />
      <FloralDecor pair={1} className="bottom-0 left-0 w-[34rem] opacity-90 sm:w-[42rem]" />
      <FloralDecor pair={1} className="bottom-0 right-0 w-[34rem] -scale-x-100 opacity-90 sm:w-[42rem]" />
      {/* Scattered whimsy for the empty 404 middle. */}
      <ScatteredIcons variant="whimsy" />
      <div className="text-center">
        <h1 className="text-4xl font-bold mb-4">404</h1>
        <p className="text-xl text-gray-600 mb-4">Oops! Page not found</p>
        <Link to="/" className="text-blue-500 hover:text-blue-700 underline">
          Return to Home
        </Link>
      </div>
    </div>
  );
};

export default NotFound;
