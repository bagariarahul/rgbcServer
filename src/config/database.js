const { Sequelize, DataTypes } = require('sequelize');
const logger = require('./logger');

// Initialize Sequelize instance
const sequelize = new Sequelize(
    process.env.DB_NAME || 'cloudbackup',
    process.env.DB_USER || 'postgres',
    process.env.DB_PASSWORD || 'password',
    {
        host: process.env.DB_HOST || 'localhost',
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

// Import all models
const User = require('../models/User')(sequelize);
const Device = require('../models/Device')(sequelize);
const SyncSession = require('../models/SyncSession')(sequelize);
const File = require('../models/File')(sequelize);
const FileChunk = require('../models/FileChunk')(sequelize);
const FilePreview = require('../models/FilePreview')(sequelize);
const SyncOperation = require('../models/SyncOperation')(sequelize);
const EncryptionKey = require('../models/EncryptionKey')(sequelize);
const StorageNode = require('../models/StorageNode')(sequelize);
const UploadQueue = require('../models/UploadQueue')(sequelize);

// Define associations
const setupAssociations = () => {
    // User associations
    User.hasMany(Device, { foreignKey: 'user_id', as: 'devices' });
    User.hasMany(SyncSession, { foreignKey: 'user_id', as: 'sessions' });
    User.hasMany(File, { foreignKey: 'user_id', as: 'files' });
    User.hasMany(SyncOperation, { foreignKey: 'user_id', as: 'syncOperations' });
    User.hasMany(EncryptionKey, { foreignKey: 'user_id', as: 'encryptionKeys' });
    User.hasMany(UploadQueue, { foreignKey: 'user_id', as: 'uploadQueue' });

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

    // EncryptionKey associations
    EncryptionKey.belongsTo(User, { foreignKey: 'user_id', as: 'user' });
    EncryptionKey.belongsTo(EncryptionKey, { foreignKey: 'rotated_from_key_id', as: 'previousKey' });
    EncryptionKey.hasMany(EncryptionKey, { foreignKey: 'rotated_from_key_id', as: 'rotatedKeys' });
    EncryptionKey.hasMany(File, { foreignKey: 'encryption_key_id', as: 'files' });

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

        // Sync database (create tables if they don't exist)
        // if (process.env.NODE_ENV === 'development') {
        //     await sequelize.sync({ alter: true });
        //     logger.info('Database synchronized');
        // } else {
        //     // In production, use migrations instead
        //     logger.info('Production mode: skipping database sync (use migrations)');
        // }
        // after you have configured `sequelize` and models & associations:
        if (process.env.DB_SYNC && process.env.DB_SYNC.toLowerCase() === 'true') {
            logger.info('DB_SYNC true; running sequelize.sync()');
            await sequelize.sync({ alter: true }); // or sync() as appropriate
        } else {
            logger.info('DB_SYNC not true; skipping sequelize.sync() (safe mode)');
        }


    } catch (error) {
        logger.error('Unable to connect to the database:', error);
        throw error;
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