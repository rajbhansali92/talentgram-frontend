import "@/index.css";
import "@/App.css";
import { Toaster } from "sonner";

export const metadata = {
  title: "Talentgram",
  description: "Casting & Talent Platform",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
      </head>
      <body className="tg-grain">
        <Toaster
          theme="dark"
          position="top-center"
          offset={16}
          mobileOffset={64}
          toastOptions={{
            style: {
              background: "#0c0c0c",
              border: "1px solid rgba(255,255,255,0.12)",
              color: "#f5f5f0",
              fontFamily: "Manrope, sans-serif",
            },
          }}
        />
        {children}
      </body>
    </html>
  );
}
