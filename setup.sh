#!/bin/bash

# Cloud Backup Server Phase 1 - Quick Start Script
# This script helps set up the development environment

set -e  # Exit on any error

echo "🚀 Cloud Backup Server Phase 1 - Quick Start"
echo "============================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
if [ "$EUID" -eq 0 ]; then 
    print_error "Please don't run this script as root"
    exit 1
fi

# Check Node.js version
print_status "Checking Node.js version..."
if ! command -v node &> /dev/null; then
    print_error "Node.js is not installed. Please install Node.js 18 or higher."
    exit 1
fi

NODE_VERSION=$(node -v | cut -d 'v' -f 2 | cut -d '.' -f 1)
if [ "$NODE_VERSION" -lt 18 ]; then
    print_error "Node.js version $NODE_VERSION is too old. Please install Node.js 18 or higher."
    exit 1
fi

print_success "Node.js $(node -v) is installed"

# Check if npm is available
if ! command -v npm &> /dev/null; then
    print_error "npm is not installed. Please install npm."
    exit 1
fi

# Install dependencies
print_status "Installing dependencies..."
npm install
print_success "Dependencies installed"

# Check for PostgreSQL
print_status "Checking PostgreSQL..."
if ! command -v psql &> /dev/null; then
    print_warning "PostgreSQL client not found. You may need to install PostgreSQL."
    echo "Ubuntu/Debian: sudo apt install postgresql postgresql-contrib"
    echo "macOS: brew install postgresql"
    echo "Windows: Download from https://postgresql.org"
else
    print_success "PostgreSQL client found"
fi

# Check for Redis
print_status "Checking Redis..."
if ! command -v redis-cli &> /dev/null; then
    print_warning "Redis client not found. You may need to install Redis."
    echo "Ubuntu/Debian: sudo apt install redis-server"
    echo "macOS: brew install redis"
    echo "Windows: Download from https://redis.io"
else
    print_success "Redis client found"
    
    # Test Redis connection
    if redis-cli ping &> /dev/null; then
        print_success "Redis is running and accessible"
    else
        print_warning "Redis is installed but not running or not accessible"
        echo "Try: sudo systemctl start redis-server (Linux)"
        echo "Or: brew services start redis (macOS)"
    fi
fi

# Check if .env file exists
if [ ! -f ".env" ]; then
    print_status "Creating .env file from template..."
    if [ -f ".env.example" ]; then
        cp .env.example .env
        print_success ".env file created"
        print_warning "Please edit .env file with your configuration:"
        echo "  - Database credentials"
        echo "  - Google OAuth credentials"
        echo "  - JWT secrets (generate secure random strings)"
    else
        print_error ".env.example file not found"
        exit 1
    fi
else
    print_success ".env file already exists"
fi

# Generate secure secrets if needed
print_status "Checking JWT secrets in .env..."
if grep -q "your_super_secure_jwt_secret" .env; then
    print_warning "Default JWT secret detected. Generating secure secrets..."
    
    # Generate secure random strings
    JWT_SECRET=$(openssl rand -base64 32)
    REFRESH_SECRET=$(openssl rand -base64 32)
    SESSION_SECRET=$(openssl rand -base64 32)
    
    # Update .env file
    sed -i.bak "s/JWT_SECRET=your_super_secure_jwt_secret_key_here_32_chars_min/JWT_SECRET=$JWT_SECRET/" .env
    sed -i.bak "s/REFRESH_TOKEN_SECRET=your_refresh_token_secret_key_here_32_chars_min/REFRESH_TOKEN_SECRET=$REFRESH_SECRET/" .env
    sed -i.bak "s/SESSION_SECRET=your_session_secret_key_here_32_chars_min/SESSION_SECRET=$SESSION_SECRET/" .env
    
    rm -f .env.bak
    print_success "Secure JWT secrets generated"
fi

# Create storage directories
print_status "Creating storage directories..."
mkdir -p storage/uploads
mkdir -p storage/temp
mkdir -p storage/previews
mkdir -p logs
print_success "Storage directories created"

# Check database connection
print_status "Testing database connection..."
if [ -f ".env" ]; then
    # Source .env file to get database credentials
    export $(cat .env | grep -v '#' | awk '/=/ {print $1}')
    
    if [ -n "$DB_HOST" ] && [ -n "$DB_USER" ] && [ -n "$DB_NAME" ]; then
        if PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1;" &> /dev/null; then
            print_success "Database connection successful"
        else
            print_warning "Database connection failed. Please check your database configuration."
            echo "Make sure PostgreSQL is running and credentials in .env are correct."
            echo "To create database manually:"
            echo "  sudo -u postgres psql"
            echo "  CREATE DATABASE $DB_NAME;"
            echo "  CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';"
            echo "  GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"
        fi
    else
        print_warning "Database configuration incomplete in .env file"
    fi
fi

# Check Google OAuth configuration
print_status "Checking Google OAuth configuration..."
if grep -q "your_google_client_id" .env; then
    print_warning "Default Google OAuth credentials detected."
    echo "Please configure Google OAuth:"
    echo "  1. Go to https://console.cloud.google.com/"
    echo "  2. Create OAuth 2.0 Client ID"
    echo "  3. Add authorized redirect URI: http://localhost:3000/api/auth/google/callback"
    echo "  4. Update GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env"
else
    print_success "Google OAuth credentials configured"
fi

echo ""
print_success "Phase 1 setup completed!"
echo ""
echo "Next steps:"
echo "  1. Review and update .env file with your configuration"
echo "  2. Ensure PostgreSQL and Redis are running"
echo "  3. Configure Google OAuth credentials"
echo "  4. Start the server: npm run dev"
echo ""
echo "Useful commands:"
echo "  npm run dev      - Start development server with auto-reload"
echo "  npm start        - Start production server"
echo "  npm test         - Run tests"
echo ""
echo "Server will be available at: http://localhost:3000"
echo "Health check: curl http://localhost:3000/health"
echo ""
print_status "For detailed setup instructions, see PHASE1_SETUP_GUIDE.md"