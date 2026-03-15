const { Sequelize } = require('sequelize');
const logger = require('./logger');

// Initialize Sequelize instance
const sequelize = new Sequelize(
    process.env.DB_NAME || 'cloudbackup',
    process.env.DB_USER || 'cloudbackup_user',
    process.env.DB_PASSWORD || 'secure_demo_password',
    {
        // FIX: Default to 'db' for Docker, fallback to localhost for local
        host: process.env.DB_HOST || 'db',
        port: process.env.DB_PORT || 5432,
        dialect: 'postgres',
        logging: (msg) => {
            if (process.env.NODE_ENV === 'development') {
                logger.debug(msg);
            }
        },
        pool: {
            max: parseInt(process.env.DB_POOL_MAX) || 10,
            min: parseInt(process.env.DB_POOL_MIN) || 0,
            acquire: parseInt(process.env.DB_POOL_ACQUIRE) || 60000,
            idle: parseInt(process.env.DB_POOL_IDLE) || 10000
        },
        define: {
            timestamps: true,
            underscored: true,
            freezeTableName: true
        }
    }
);

// Import all models - ORDER MATTERS for table creation!
// 1. Base models (no dependencies)
const User = require('../models/User')(sequelize);

// 2. First-level dependencies
const EncryptionKey = require('../models/EncryptionKey')(sequelize); // MOVED UP: Must exist before File
const Device = require('../models/Device')(sequelize);               // MOVED UP: Must exist before File/Session

// 3. Dependent models
const SyncSession = require('../models/SyncSession')(sequelize);
const File = require('../models/File')(sequelize);                   // References User, Device, EncryptionKey

// 4. Deep dependencies
const FileChunk = require('../models/FileChunk')(sequelize);
const FilePreview = require('../models/FilePreview')(sequelize);
const SyncOperation = require('../models/SyncOperation')(sequelize);
const UploadQueue = require('../models/UploadQueue')(sequelize);
const StorageNode = require('../models/StorageNode')(sequelize);

// Define associations
const setupAssociations = () => {
    // User associations
    User.hasMany(Device, { foreignKey: 'user_id', as: 'devices' });
    User.hasMany(SyncSession, { foreignKey: 'user_id', as: 'sessions' });
    User.hasMany(File, { foreignKey: 'user_id', as: 'files' });
    User.hasMany(SyncOperation, { foreignKey: 'user_id', as: 'syncOperations' });
    User.hasMany(EncryptionKey, { foreignKey: 'user_id', as: 'encryptionKeys' });
    User.hasMany(UploadQueue, { foreignKey: 'user_id', as: 'uploadQueue' });

    // EncryptionKey associations
    EncryptionKey.belongsTo(User, { foreignKey: 'user_id', as: 'user' });
    EncryptionKey.belongsTo(EncryptionKey, { foreignKey: 'rotated_from_key_id', as: 'previousKey' });
    EncryptionKey.hasMany(EncryptionKey, { foreignKey: 'rotated_from_key_id', as: 'rotatedKeys' });
    EncryptionKey.hasMany(File, { foreignKey: 'encryption_key_id', as: 'files' });

    // Device associations
    Device.belongsTo(User, { foreignKey: 'user_id', as: 'user' });
    Device.hasMany(SyncSession, { foreignKey: 'device_id', as: 'sessions' });
    Device.hasMany(File, { foreignKey: 'device_id', as: 'files' });
    Device.hasMany(SyncOperation, { foreignKey: 'device_id', as: 'syncOperations' });

    // SyncSession associations
    SyncSession.belongsTo(User, { foreignKey: 'user_id', as: 'user' });
    SyncSession.belongsTo(Device, { foreignKey: 'device_id', as: 'device' });

    // File associations
    File.belongsTo(User, { foreignKey: 'user_id', as: 'user' });
    File.belongsTo(Device, { foreignKey: 'device_id', as: 'device' });
    File.belongsTo(EncryptionKey, { foreignKey: 'encryption_key_id', as: 'encryptionKey' });
    File.belongsTo(File, { foreignKey: 'parent_file_id', as: 'parentFile' });
    File.hasMany(File, { foreignKey: 'parent_file_id', as: 'versions' });
    File.hasMany(FileChunk, { foreignKey: 'file_id', as: 'chunks' });
    File.hasMany(FilePreview, { foreignKey: 'file_id', as: 'previews' });
    File.hasMany(SyncOperation, { foreignKey: 'file_id', as: 'syncOperations' });

    // FileChunk associations
    FileChunk.belongsTo(File, { foreignKey: 'file_id', as: 'file' });

    // FilePreview associations
    FilePreview.belongsTo(File, { foreignKey: 'file_id', as: 'file' });

    // SyncOperation associations
    SyncOperation.belongsTo(User, { foreignKey: 'user_id', as: 'user' });
    SyncOperation.belongsTo(Device, { foreignKey: 'device_id', as: 'device' });
    SyncOperation.belongsTo(File, { foreignKey: 'file_id', as: 'file' });

    // UploadQueue associations
    UploadQueue.belongsTo(User, { foreignKey: 'user_id', as: 'user' });
    UploadQueue.belongsTo(File, { foreignKey: 'file_id', as: 'file' });

    logger.info('Database associations configured');
};

// Database connection function
const connectDB = async () => {
    try {
        // Test the connection
        await sequelize.authenticate();
        logger.info('Database connection established successfully');

        // Set up model associations
        setupAssociations();

        // ── Safe startup sync ──────────────────────────────────────────
        // force: false  → never drops tables (preserves all data)
        // alter: false  → never modifies existing columns
        //
        // This will ONLY create tables that don't exist yet.
        // All schema modifications must be handled via Sequelize CLI
        // migrations (npx sequelize-cli migration:generate).
        //
        // DB_RESET and DB_SYNC flags have been permanently removed.
        // ────────────────────────────────────────────────────────────────
        await sequelize.sync({ force: false, alter: false });
        logger.info('Database tables verified (sync: force=false, alter=false)');

    } catch (error) {
        logger.error('Unable to connect to the database:', error);
        // We don't throw here so the server can still start in "offline" mode if needed
    }
};

// Health check function
const checkDBHealth = async () => {
    try {
        await sequelize.authenticate();
        return { status: 'healthy', connection: 'active' };
    } catch (error) {
        return { status: 'unhealthy', error: error.message };
    }
};

// Graceful shutdown
const closeDB = async () => {
    try {
        await sequelize.close();
        logger.info('Database connection closed');
    } catch (error) {
        logger.error('Error closing database connection:', error);
    }
};

// Export models and functions
module.exports = {
    sequelize,
    connectDB,
    checkDBHealth,
    closeDB,
    // Models
    User,
    Device,
    SyncSession,
    File,
    FileChunk,
    FilePreview,
    SyncOperation,
    EncryptionKey,
    StorageNode,
    UploadQueue
};