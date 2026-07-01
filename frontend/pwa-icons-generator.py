import os
from PIL import Image

def generate_icons():
    logo_path = "/Users/rajrbhansali/.gemini/antigravity/scratch/talentgram-frontend/frontend/public/brand/talentgram-white.png"
    public_dir = "/Users/rajrbhansali/.gemini/antigravity/scratch/talentgram-frontend/frontend/public"
    
    if not os.path.exists(logo_path):
        print(f"Error: Logo file not found at {logo_path}")
        return
        
    logo = Image.open(logo_path)
    orig_w, orig_h = logo.size
    aspect_ratio = orig_w / orig_h
    
    sizes = [72, 96, 128, 144, 152, 180, 192, 256, 384, 512]
    
    for size in sizes:
        # 1. Standard Icon
        # Standard icon uses 85% width for the logo
        img = Image.new("RGBA", (size, size), (0, 0, 0, 255))
        w = int(size * 0.85)
        h = int(w / aspect_ratio)
        if h > size * 0.85:
            h = int(size * 0.85)
            w = int(h * aspect_ratio)
            
        logo_resized = logo.resize((w, h), Image.Resampling.LANCZOS)
        x = (size - w) // 2
        y = (size - h) // 2
        img.paste(logo_resized, (x, y), logo_resized)
        
        # Save as PNG
        out_path = os.path.join(public_dir, f"icon-{size}.png")
        img.convert("RGB").save(out_path, "PNG")
        print(f"Generated standard icon: {out_path}")
        
        # For apple-touch-icon, also save it as apple-touch-icon.png
        if size == 180:
            apple_path = os.path.join(public_dir, "apple-touch-icon.png")
            img.convert("RGB").save(apple_path, "PNG")
            # Also save in src/app for Next.js metadata fallback
            app_apple_path = "/Users/rajrbhansali/.gemini/antigravity/scratch/talentgram-frontend/frontend/src/app/apple-touch-icon.png"
            img.convert("RGB").save(app_apple_path, "PNG")
            print(f"Generated apple-touch-icon: {apple_path} and {app_apple_path}")
            
        # 2. Maskable Icon (for 192 and 512)
        if size in [192, 512]:
            # Maskable icon needs to fit inside the safe circular area (minimum 40% padding, logo <= 60% of size)
            img_maskable = Image.new("RGBA", (size, size), (0, 0, 0, 255))
            w_m = int(size * 0.60)
            h_m = int(w_m / aspect_ratio)
            if h_m > size * 0.60:
                h_m = int(size * 0.60)
                w_m = int(h_m * aspect_ratio)
                
            logo_resized_m = logo.resize((w_m, h_m), Image.Resampling.LANCZOS)
            x_m = (size - w_m) // 2
            y_m = (size - h_m) // 2
            img_maskable.paste(logo_resized_m, (x_m, y_m), logo_resized_m)
            
            out_maskable_path = os.path.join(public_dir, f"icon-{size}-maskable.png")
            img_maskable.convert("RGB").save(out_maskable_path, "PNG")
            print(f"Generated maskable icon: {out_maskable_path}")

    # Generate standard favicon (32x32)
    fav = Image.new("RGBA", (32, 32), (0, 0, 0, 255))
    w_f = int(32 * 0.85)
    h_f = int(w_f / aspect_ratio)
    logo_resized_f = logo.resize((w_f, h_f), Image.Resampling.LANCZOS)
    fav.paste(logo_resized_f, ((32 - w_f) // 2, (32 - h_f) // 2), logo_resized_f)
    fav.convert("RGBA").save(os.path.join(public_dir, "favicon.ico"), "ICO")
    
    # Also save favicon in src/app
    fav.convert("RGBA").save("/Users/rajrbhansali/.gemini/antigravity/scratch/talentgram-frontend/frontend/src/app/favicon.ico", "ICO")
    print("Generated favicon.ico")

if __name__ == "__main__":
    generate_icons()
