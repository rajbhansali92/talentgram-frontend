export default function RootLayout({ children }) {
  return (
    <html>
      <body>
        <div style={{
          background: 'red',
          color: 'white',
          minHeight: '100vh',
          fontSize: '60px'
        }}>
          ROOT LAYOUT WORKS
        </div>
      </body>
    </html>
  );
}
