# Cloud Backup Server - Phase 1 Setup

This guide will help you set up the Phase 1 server infrastructure with Google OAuth and proper queue mechanisms.

## Prerequisites

1. **Node.js** (v18 or higher)
2. **PostgreSQL** (v13 or higher)
3. **Redis** (v6 or higher)
4. **Google OAuth Credentials**

## Quick Setup

### 1. Install Dependencies

```bash
npm install
```

### 2. Database Setup

Create PostgreSQL database and user:

```sql
-- Connect to PostgreSQL as superuser
sudo -u postgres psql

-- Create database and user
CREATE DATABASE cloudbackup;
CREATE USER cloudbackup_user WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE cloudbackup TO cloudbackup_user;
GRANT ALL ON SCHEMA public TO cloudbackup_user;
```

### 3. Redis Setup

Install and start Redis:

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install redis-server

# Start Redis
sudo systemctl start redis-server
sudo systemctl enable redis-server

# Test Redis
redis-cli ping
```

### 4. Google OAuth Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing one
3. Enable Google+ API
4. Go to "Credentials" → "Create Credentials" → "OAuth 2.0 Client ID"
5. Configure OAuth consent screen
6. Add authorized redirect URIs:
   - `http://localhost:3000/api/auth/google/callback`
   - Your production domain callback URL

### 5. Environment Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Edit `.env` with your settings:

```env
# Database Configuration
DB_HOST=localhost
DB_PORT=5432
DB_NAME=cloudbackup
DB_USER=cloudbackup_user
DB_PASSWORD=your_secure_password

# Google OAuth Configuration
GOOGLE_CLIENT_ID=your_google_client_id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your_google_client_secret

# JWT Secrets (generate secure random strings)
JWT_SECRET=your_super_secure_jwt_secret_key_here_32_chars_min
REFRESH_TOKEN_SECRET=your_refresh_token_secret_key_here_32_chars_min
SESSION_SECRET=your_session_secret_key_here_32_chars_min
```

### 6. Start the Server

```bash
# Development mode with auto-reload
npm run dev

# Production mode
npm start
```

The server will start at `http://localhost:3000`

## API Endpoints

### Authentication

#### Register
```http
POST /api/auth/register
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "SecurePass123!",
  "firstName": "John",
  "lastName": "Doe",
  "deviceName": "My Phone",
  "deviceType": "ANDROID",
  "deviceId": "uuid-generated-by-android"
}
```

#### Login
```http
POST /api/auth/login
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "SecurePass123!",
  "deviceName": "My Phone",
  "deviceType": "ANDROID",
  "deviceId": "uuid-generated-by-android"
}
```

#### Google OAuth
```http
GET /api/auth/google
```

#### Token Refresh
```http
POST /api/auth/refresh
Content-Type: application/json

{
  "refreshToken": "your_refresh_token"
}
```

#### Get User Info
```http
GET /api/auth/me
Authorization: Bearer your_access_token
```

#### Logout
```http
POST /api/auth/logout
Authorization: Bearer your_access_token
```

## Android Integration

### 1. Update NetworkModule.kt

```kotlin
// In your di/NetworkModule.kt
@Provides
@Singleton
fun provideAuthApiService(retrofit: Retrofit): AuthApiService {
    return retrofit.create(AuthApiService::class.java)
}

// Update base URL
@Provides
@Singleton
fun provideRetrofit(okHttpClient: OkHttpClient): Retrofit {
    return Retrofit.Builder()
        .baseUrl("http://10.0.2.2:3000/api/") // For emulator
        // .baseUrl("http://your-server-ip:3000/api/") // For real device
        .client(okHttpClient)
        .addConverterFactory(GsonConverterFactory.create())
        .build()
}
```

### 2. Update AuthApiService.kt

```kotlin
interface AuthApiService {
    @POST("auth/register")
    suspend fun register(@Body request: RegisterRequest): Response<AuthResponse>

    @POST("auth/login")
    suspend fun login(@Body request: LoginRequest): Response<AuthResponse>

    @POST("auth/refresh")
    suspend fun refreshToken(@Body request: RefreshTokenRequest): Response<TokenResponse>

    @GET("auth/me")
    suspend fun getCurrentUser(): Response<UserResponse>

    @POST("auth/logout")
    suspend fun logout(): Response<MessageResponse>
}

data class RegisterRequest(
    val email: String,
    val password: String,
    val firstName: String,
    val lastName: String,
    val deviceName: String,
    val deviceType: String,
    val deviceId: String
)

data class LoginRequest(
    val email: String,
    val password: String,
    val deviceName: String,
    val deviceType: String,
    val deviceId: String
)

data class AuthResponse(
    val message: String,
    val user: User,
    val tokens: Tokens,
    val session: Session,
    val device: Device
)

data class Tokens(
    val accessToken: String,
    val refreshToken: String
)
```

### 3. Update AuthManager.kt

```kotlin
@Singleton
class AuthManager @Inject constructor(
    private val authApiService: AuthApiService,
    private val tokenStorage: TokenStorage,
    private val deviceInfoProvider: DeviceInfoProvider
) {
    suspend fun register(
        email: String,
        password: String,
        firstName: String,
        lastName: String
    ): Result<AuthResponse> {
        return try {
            val deviceInfo = deviceInfoProvider.getDeviceInfo()
            val request = RegisterRequest(
                email = email,
                password = password,
                firstName = firstName,
                lastName = lastName,
                deviceName = deviceInfo.name,
                deviceType = "ANDROID",
                deviceId = deviceInfo.id
            )

            val response = authApiService.register(request)
            if (response.isSuccessful) {
                response.body()?.let { authResponse ->
                    tokenStorage.saveTokens(
                        authResponse.tokens.accessToken,
                        authResponse.tokens.refreshToken
                    )
                    Result.success(authResponse)
                } ?: Result.failure(Exception("Empty response"))
            } else {
                Result.failure(Exception(response.message()))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun login(email: String, password: String): Result<AuthResponse> {
        return try {
            val deviceInfo = deviceInfoProvider.getDeviceInfo()
            val request = LoginRequest(
                email = email,
                password = password,
                deviceName = deviceInfo.name,
                deviceType = "ANDROID",
                deviceId = deviceInfo.id
            )

            val response = authApiService.login(request)
            if (response.isSuccessful) {
                response.body()?.let { authResponse ->
                    tokenStorage.saveTokens(
                        authResponse.tokens.accessToken,
                        authResponse.tokens.refreshToken
                    )
                    Result.success(authResponse)
                } ?: Result.failure(Exception("Empty response"))
            } else {
                Result.failure(Exception(response.message()))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
}
```

### 4. Create DeviceInfoProvider.kt

```kotlin
@Singleton
class DeviceInfoProvider @Inject constructor(
    @ApplicationContext private val context: Context
) {
    private val deviceId: String by lazy {
        val sharedPrefs = context.getSharedPreferences("device_info", Context.MODE_PRIVATE)
        var id = sharedPrefs.getString("device_id", null)
        
        if (id == null) {
            id = UUID.randomUUID().toString()
            sharedPrefs.edit().putString("device_id", id).apply()
        }
        
        id
    }

    fun getDeviceInfo(): DeviceInfo {
        return DeviceInfo(
            id = deviceId,
            name = "${Build.MANUFACTURER} ${Build.MODEL}",
            type = "ANDROID",
            osVersion = Build.VERSION.RELEASE,
            appVersion = getAppVersion()
        )
    }

    private fun getAppVersion(): String {
        return try {
            val packageInfo = context.packageManager.getPackageInfo(context.packageName, 0)
            packageInfo.versionName ?: "1.0.0"
        } catch (e: Exception) {
            "1.0.0"
        }
    }
}

data class DeviceInfo(
    val id: String,
    val name: String,
    val type: String,
    val osVersion: String,
    val appVersion: String
)
```

## Testing the Setup

### 1. Health Check
```bash
curl http://localhost:3000/health
```

### 2. Register Test User
```bash
curl -X POST http://localhost:3000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "TestPass123!",
    "firstName": "Test",
    "lastName": "User",
    "deviceName": "Test Device",
    "deviceType": "ANDROID",
    "deviceId": "test-uuid-123"
  }'
```

### 3. Login Test
```bash
curl -X POST http://localhost:3000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "TestPass123!",
    "deviceType": "ANDROID",
    "deviceId": "test-uuid-123"
  }'
```

## Queue Management

The server includes a comprehensive queue system for handling failed uploads and retries:

### Queue Types
- **Upload Queue**: Handles chunked file uploads with retry logic
- **Processing Queue**: File processing, encryption, virus scanning
- **Preview Queue**: Thumbnail and preview generation
- **Sync Queue**: Real-time sync notifications
- **Cleanup Queue**: Cleanup expired jobs and temp files

### Queue Monitoring

You can check queue status at any time:

```javascript
// In your server console
const { QueueManager } = require('./src/config/queues');

// Get queue statistics
const stats = await QueueManager.getQueueStats();
console.log(stats);
```

## Next Steps

After Phase 1 is working:

1. **Phase 2**: Implement chunked file uploads and file processing
2. **Phase 3**: Add WebSocket real-time sync
3. **Phase 4**: Implement preview generation and RAID storage
4. **Phase 5**: Add comprehensive monitoring and admin interface

## Troubleshooting

### Database Connection Issues
```bash
# Check PostgreSQL status
sudo systemctl status postgresql

# Check database exists
sudo -u postgres psql -c "\l" | grep cloudbackup
```

### Redis Connection Issues
```bash
# Check Redis status
sudo systemctl status redis-server

# Test Redis connection
redis-cli ping
```

### Port Issues
```bash
# Check if port 3000 is in use
lsof -i :3000

# Kill process using port 3000
kill -9 $(lsof -t -i:3000)
```

### Google OAuth Issues
- Verify OAuth consent screen is configured
- Check authorized redirect URIs match exactly
- Ensure Google+ API is enabled
- Verify client ID and secret are correct

## Security Considerations

1. **Always use HTTPS in production**
2. **Configure proper CORS origins**
3. **Use strong JWT secrets (32+ characters)**
4. **Regularly rotate secrets**
5. **Monitor failed authentication attempts**
6. **Set up proper firewall rules**
7. **Keep dependencies updated**

The Phase 1 server provides a solid foundation with proper authentication, session management, and queue systems ready for the next phases of development.