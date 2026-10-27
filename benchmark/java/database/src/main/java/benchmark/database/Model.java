package benchmark.database;

import jakarta.persistence.*;
import java.util.ArrayList;
import java.util.List;

public final class Model {
    private Model() {}
    @Entity(name="BenchCustomer") @Table(name="customer")
    public static class Customer {
        @Id @Column(name="id") public long id;
        @Column(name="email",nullable=false,length=254) public String email;
        @Column(name="name",nullable=false,length=128) public String name;
        @Column(name="balance_cents",nullable=false) public long balanceCents;
        @Version @Column(name="version",nullable=false) public long version;
        public Customer() {}
    }
    @Entity(name="BenchProduct") @Table(name="product")
    public static class Product {
        @Id @Column(name="id") public long id;
        @Column(name="sku",nullable=false,length=64) public String sku;
        @Column(name="name",nullable=false,length=128) public String name;
        @Column(name="price_cents",nullable=false) public long priceCents;
        public Product() {}
    }
    @Entity(name="BenchOrder") @Table(name="orders")
    public static class Order {
        @Id @Column(name="id") public long id;
        @Column(name="customer_id",nullable=false) public long customerId;
        @Column(name="status",nullable=false) public long status;
        @Column(name="created_epoch_ms",nullable=false) public long createdEpochMs;
        @Column(name="total_cents",nullable=false) public long totalCents;
        @Version @Column(name="version",nullable=false) public long version;
        @ManyToOne(fetch=FetchType.LAZY) @JoinColumn(name="customer_id",insertable=false,updatable=false) public Customer customer;
        @OneToMany(mappedBy="order",fetch=FetchType.LAZY) @OrderBy("lineNo ASC") public List<Line> lines = new ArrayList<>();
        public Order() {}
    }
    @Entity(name="BenchLine") @Table(name="order_line")
    public static class Line {
        @Id @Column(name="id") public long id;
        @Column(name="order_id",nullable=false) public long orderId;
        @Column(name="line_no",nullable=false) public long lineNo;
        @Column(name="product_id",nullable=false) public long productId;
        @Column(name="quantity",nullable=false) public long quantity;
        @Column(name="unit_price_cents",nullable=false) public long unitPriceCents;
        @ManyToOne(fetch=FetchType.LAZY) @JoinColumn(name="order_id",insertable=false,updatable=false) public Order order;
        public Line() {}
    }
}
