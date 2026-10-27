using Microsoft.EntityFrameworkCore;
using System.Data.Common;

namespace BenchmarkDatabase;

public sealed class Customer
{
    public long Id { get; set; }
    public string Email { get; set; } = "";
    public string Name { get; set; } = "";
    public long BalanceCents { get; set; }
    public long Version { get; set; }
}
public sealed class Product
{
    public long Id { get; set; }
    public string Sku { get; set; } = "";
    public string Name { get; set; } = "";
    public long PriceCents { get; set; }
}
public sealed class Order
{
    public long Id { get; set; }
    public long CustomerId { get; set; }
    public long Status { get; set; }
    public long CreatedEpochMs { get; set; }
    public long TotalCents { get; set; }
    public long Version { get; set; }
    public Customer Customer { get; set; } = null!;
    public List<OrderLine> Lines { get; set; } = [];
}
public sealed class OrderLine
{
    public long Id { get; set; }
    public long OrderId { get; set; }
    public long LineNo { get; set; }
    public long ProductId { get; set; }
    public long Quantity { get; set; }
    public long UnitPriceCents { get; set; }
}

public sealed class BenchContext : DbContext
{
    public BenchContext(string engine, DbConnection connection) : base(Options(engine, connection)) { }
    public DbSet<Customer> Customers => Set<Customer>();
    public DbSet<Product> Products => Set<Product>();
    public DbSet<Order> Orders => Set<Order>();
    public DbSet<OrderLine> Lines => Set<OrderLine>();

    private static DbContextOptions Options(string engine, DbConnection c)
    {
        var b = new DbContextOptionsBuilder();
        switch (engine)
        {
            case "sqlite": b.UseSqlite(c, o => o.CommandTimeout(15)); break;
            case "postgresql": b.UseNpgsql(c, o => o.MaxBatchSize(1).CommandTimeout(15)); break;
            case "sqlserver": b.UseSqlServer(c, o => o.MaxBatchSize(1).CommandTimeout(15)); break;
            default: throw new ArgumentException("Unknown engine");
        }
        b.UseQueryTrackingBehavior(QueryTrackingBehavior.TrackAll);
        return b.Options;
    }
    protected override void OnModelCreating(ModelBuilder b)
    {
        var c = b.Entity<Customer>(); c.ToTable("customer"); c.HasKey(x => x.Id);
        c.Property(x => x.Id).HasColumnName("id").ValueGeneratedNever();
        c.Property(x => x.Email).HasColumnName("email").HasMaxLength(254).IsUnicode(false).IsRequired();
        c.Property(x => x.Name).HasColumnName("name").HasMaxLength(128).IsUnicode(false).IsRequired();
        c.Property(x => x.BalanceCents).HasColumnName("balance_cents");
        c.Property(x => x.Version).HasColumnName("version").IsConcurrencyToken();
        var p = b.Entity<Product>(); p.ToTable("product"); p.HasKey(x => x.Id);
        p.Property(x => x.Id).HasColumnName("id").ValueGeneratedNever();
        p.Property(x => x.Sku).HasColumnName("sku").HasMaxLength(64).IsUnicode(false).IsRequired();
        p.Property(x => x.Name).HasColumnName("name").HasMaxLength(128).IsUnicode(false).IsRequired();
        p.Property(x => x.PriceCents).HasColumnName("price_cents");
        var o = b.Entity<Order>(); o.ToTable("orders"); o.HasKey(x => x.Id);
        o.Property(x => x.Id).HasColumnName("id").ValueGeneratedNever();
        o.Property(x => x.CustomerId).HasColumnName("customer_id"); o.Property(x => x.Status).HasColumnName("status");
        o.Property(x => x.CreatedEpochMs).HasColumnName("created_epoch_ms"); o.Property(x => x.TotalCents).HasColumnName("total_cents");
        o.Property(x => x.Version).HasColumnName("version").IsConcurrencyToken();
        o.HasOne(x => x.Customer).WithMany().HasForeignKey(x => x.CustomerId).OnDelete(DeleteBehavior.NoAction);
        o.HasMany(x => x.Lines).WithOne().HasForeignKey(x => x.OrderId).OnDelete(DeleteBehavior.NoAction);
        var l = b.Entity<OrderLine>(); l.ToTable("order_line"); l.HasKey(x => x.Id);
        l.Property(x => x.Id).HasColumnName("id").ValueGeneratedNever(); l.Property(x => x.OrderId).HasColumnName("order_id");
        l.Property(x => x.LineNo).HasColumnName("line_no"); l.Property(x => x.ProductId).HasColumnName("product_id");
        l.Property(x => x.Quantity).HasColumnName("quantity"); l.Property(x => x.UnitPriceCents).HasColumnName("unit_price_cents");
    }
}
